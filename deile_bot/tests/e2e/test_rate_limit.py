"""E2E-4 — rate limit scenarios."""

from __future__ import annotations

import pytest

from deile_bot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope, make_user)
from deile_bot.foundation.audit import BotAuditLogger
from deile_bot.foundation.capabilities import CapabilityCatalog
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
from deile_bot.foundation.settings import BotSettings, FoundationSettings
from deile_bot.tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def test_e2e4_burst_then_blocked(store):
    settings = BotSettings(
        foundation=FoundationSettings(rate_limit_user_burst=3)
    )
    adapter = FakeProviderAdapter()
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    bridge = CapturingFakeBridge()
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl, store=store, audit=audit, event_bus=bus, metrics=metrics, dlq=dlq,
    )
    pipeline = IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store,
        intent=HeuristicIntentClassifier(), bridge=bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit, event_bus=bus, metrics=metrics, egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    user = make_user(provider="fake", provider_user_id="rl1")
    for i in range(8):
        env = make_envelope(
            channel=make_channel(provider="fake", scope=ChannelScope.DM),
            author=user,
            text="oi DEILE",
            message_id=f"rl-{i}",
        )
        await pipeline.handle(env, adapter)
    snap = metrics.snapshot()
    rate_limited = snap["counters"].get("bot_rate_limited_total", [])
    audit_rl = await store.query_audit(event_type="rate_limited")
    assert (rate_limited and any(s["value"] >= 1 for s in rate_limited)) or audit_rl
