"""Unit tests for SingleProviderRuntime + MultiProviderRuntime."""

from __future__ import annotations

import pytest

from deile_bot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_envelope)
from deile_bot.foundation.audit import BotAuditLogger
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.dlq import DeadLetterQueue
from deile_bot.foundation.event_bus import BotEventBus
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.intent import HeuristicIntentClassifier
from deile_bot.foundation.metrics import MetricsCollector
from deile_bot.foundation.output_formatter import PlainTextFormatter
from deile_bot.foundation.permissions import PermissionGate
from deile_bot.foundation.persona_selector import PersonaSelector
from deile_bot.foundation.pipeline import EgressPipeline, IngressPipeline
from deile_bot.foundation.rate_limit import RateLimiter
from deile_bot.foundation.settings import BotSettings
from deile_bot.runtime.single_runtime import (MultiProviderRuntime,
                                              SingleProviderRuntime)


class _FakeBridge:
    async def invoke(self, inv):
        from deile.common.markup_ast import MarkupAST
        from deile_bot.foundation.agent_bridge import AgentResponse
        return AgentResponse(
            text="hello back from fake bridge with enough chars",
            markup=MarkupAST.from_plain("hello back"),
            elapsed_ms=5,
            model_used="fake",
        )


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "rt.sqlite")
    await s.init()
    yield s
    await s.close()


def _build_pipeline(store_, adapter):
    settings = BotSettings()
    identity = IdentityResolver(store_)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store_)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store_, settings)
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl, store=store_, audit=audit, event_bus=bus,
        metrics=metrics, dlq=dlq,
    )
    return IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store_,
        intent=HeuristicIntentClassifier(), bridge=_FakeBridge(),
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit, event_bus=bus, metrics=metrics, egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )


class TestSingleProviderRuntime:
    async def test_start_wires_callback(self, store):
        adapter = FakeProviderAdapter()
        pipeline = _build_pipeline(store, adapter)
        rt = SingleProviderRuntime(adapter, pipeline)
        await rt.start()
        assert adapter.on_inbound is not None
        await rt.stop()

    async def test_inject_routes_through_pipeline(self, store):
        adapter = FakeProviderAdapter()
        pipeline = _build_pipeline(store, adapter)
        rt = SingleProviderRuntime(adapter, pipeline)
        await rt.start()
        try:
            from deile_bot._testing import make_channel, make_user
            from deile_bot.foundation.envelope import ChannelScope
            env = make_envelope(
                text="oi tudo bem amigo",
                author=make_user(),
                channel=make_channel(scope=ChannelScope.DM),
            )
            await adapter.inject(env)
            # Bridge's response went through egress → adapter.inbox has it
            assert len(adapter.inbox) >= 1
        finally:
            await rt.stop()

    async def test_pipeline_exception_is_logged_not_raised(self, store):
        adapter = FakeProviderAdapter()
        pipeline = _build_pipeline(store, adapter)

        class _FailingPipeline:
            capability_catalog = pipeline.capability_catalog
            agent_meta = pipeline.agent_meta

            async def handle(self, env, src):
                raise RuntimeError("boom")

        rt = SingleProviderRuntime(adapter, _FailingPipeline())
        await rt.start()
        try:
            from deile_bot._testing import make_channel
            from deile_bot.foundation.envelope import ChannelScope
            env = make_envelope(channel=make_channel(scope=ChannelScope.DM))
            # Should NOT raise
            await adapter.inject(env)
        finally:
            await rt.stop()


class TestMultiProviderRuntime:
    async def test_start_wires_all_adapters(self, store):
        adapters = [FakeProviderAdapter(), FakeProviderAdapter()]
        # Each adapter shares the pipeline (use first adapter's)
        pipeline = _build_pipeline(store, adapters[0])
        rt = MultiProviderRuntime(adapters, pipeline)
        await rt.start()
        for a in adapters:
            assert a.on_inbound is not None
        await rt.stop()

    async def test_stop_swallows_adapter_errors(self, store):
        class _BadAdapter(FakeProviderAdapter):
            async def stop(self):
                raise RuntimeError("adapter crash on stop")

        bad = _BadAdapter()
        good = FakeProviderAdapter()
        pipeline = _build_pipeline(store, good)
        rt = MultiProviderRuntime([bad, good], pipeline)
        await rt.start()
        # Should NOT raise
        await rt.stop()
