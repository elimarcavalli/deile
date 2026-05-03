"""Tests for IngressPipeline + EgressPipeline (unit-level)."""

from __future__ import annotations

import pytest

from deile_bot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope, make_user)
from deile_bot.foundation.agent_bridge import (AgentBridge, AgentInvocation,
                                               AgentResponse)
from deile_bot.foundation.audit import BotAuditLogger
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.dlq import DeadLetterQueue
from deile_bot.foundation.envelope import ChannelScope
from deile_bot.foundation.event_bus import BotEventBus
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.intent import HeuristicIntentClassifier
from deile_bot.foundation.metrics import MetricsCollector
from deile_bot.foundation.output_formatter import PlainTextFormatter
from deile_bot.foundation.permissions import PermissionGate
from deile_bot.foundation.persona_selector import PersonaSelector
from deile_bot.foundation.pipeline import EgressPipeline, IngressPipeline
from deile_bot.foundation.rate_limit import RateLimiter
from deile_bot.foundation.settings import BotSettings, PermissionsSettings


class FakeBridge(AgentBridge):
    def __init__(self, response_text="canned response", *, raise_exc=None):
        self.response_text = response_text
        self.invocations = []
        self._raise = raise_exc

    async def invoke(self, inv: AgentInvocation) -> AgentResponse:
        self.invocations.append(inv)
        if self._raise:
            raise self._raise
        from deile.common.markup_ast import MarkupAST

        return AgentResponse(
            text=self.response_text,
            markup=MarkupAST.from_plain(self.response_text),
            elapsed_ms=10,
            model_used="fake",
        )


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "p.sqlite")
    await s.init()
    yield s
    await s.close()


def _wire(store, *, settings=None, bridge=None, adapter=None):
    settings = settings or BotSettings()
    bridge = bridge or FakeBridge()
    adapter = adapter or FakeProviderAdapter()
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl,
        store=store,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        dlq=dlq,
    )
    catalog = CapabilityCatalog()
    persona = PersonaSelector(settings, identity)
    intent = HeuristicIntentClassifier()
    meta = FakeAgentMetaProvider()
    pipeline = IngressPipeline(
        identity=identity,
        permissions=perms,
        rate_limit=rl,
        store=store,
        intent=intent,
        bridge=bridge,
        capability_catalog=catalog,
        persona_selector=persona,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        egress=egress,
        agent_meta=meta,
    )
    return pipeline, adapter, bridge, store, metrics


class TestHappyPath:
    async def test_dm_response_lands_in_inbox(self, store):
        pipeline, adapter, bridge, _, metrics = _wire(store)
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.DM), text="oi DEILE"
        )
        await pipeline.handle(env, adapter)
        assert len(adapter.inbox) == 1
        assert adapter.inbox[0]["text"] == "canned response"
        snap = metrics.snapshot()
        assert any(s["value"] >= 1 for s in snap["counters"].get("bot_inbound_total", []))

    async def test_history_persists(self, store):
        pipeline, adapter, bridge, _, _ = _wire(store)
        env1 = make_envelope(
            channel=make_channel(scope=ChannelScope.DM, provider_channel_id="dm1"),
            text="primeiro",
            message_id="m1",
        )
        env2 = make_envelope(
            channel=make_channel(scope=ChannelScope.DM, provider_channel_id="dm1"),
            author=env1.author,
            text="segundo",
            message_id="m2",
        )
        await pipeline.handle(env1, adapter)
        await pipeline.handle(env2, adapter)
        ch = make_channel(scope=ChannelScope.DM, provider_channel_id="dm1")
        msgs = await store.get_recent_messages("fake", ch)
        assert len(msgs) >= 4  # 2 in + 2 out


class TestIntent:
    async def test_noise_in_group_skipped(self, store):
        pipeline, adapter, bridge, _, _ = _wire(store)
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.GROUP),
            text="random talk no one cares",
        )
        await pipeline.handle(env, adapter)
        assert adapter.inbox == []
        assert bridge.invocations == []


class TestPermissionDenied:
    async def test_denied_user_no_invoke(self, store):
        # Pre-resolve user so its bot_user_id is stable across the test.
        u = make_user(bot_user_id="BLOCK", provider_user_id="999")
        await store.upsert_user(u)
        settings = BotSettings(permissions=PermissionsSettings(blocklist=["BLOCK"]))
        pipeline, adapter, bridge, _, _ = _wire(store, settings=settings)
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.DM), author=u, text="oi"
        )
        await pipeline.handle(env, adapter)
        assert adapter.inbox == []
        rows = await store.query_audit(event_type="permission_denied")
        assert any(r["bot_user_id"] for r in rows)


class TestAgentFailure:
    async def test_agent_error_sends_fallback(self, store):
        from deile_bot.foundation.exceptions import AgentInvocationError

        bridge = FakeBridge(raise_exc=AgentInvocationError("nope"))
        pipeline, adapter, _, _, _ = _wire(store, bridge=bridge)
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM), text="hi")
        await pipeline.handle(env, adapter)
        # fallback message lands in inbox (warn icon)
        assert len(adapter.inbox) == 1
        assert "agent_failed" in adapter.inbox[0]["text"]
        rows = await store.query_audit(event_type="agent_failed")
        assert len(rows) >= 1


class TestEgressDLQ:
    async def test_send_failure_lands_in_dlq(self, store):
        adapter = FakeProviderAdapter()
        adapter.fail_send_n_times(10)
        pipeline, adapter, bridge, _, metrics = _wire(store, adapter=adapter)
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM), text="hi")
        await pipeline.handle(env, adapter)
        assert await DLQ_count(store) >= 1


async def DLQ_count(store):
    return await store.count_dlq()
