"""E2E-7 — intent modes (heuristic, always_respond_to_addressed, always_respond)."""

from __future__ import annotations

import pytest

from deile_bot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope)
from deile_bot.foundation.audit import BotAuditLogger
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.dlq import DeadLetterQueue
from deile_bot.foundation.envelope import ChannelScope
from deile_bot.foundation.event_bus import BotEventBus
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.intent import build_intent_classifier
from deile_bot.foundation.metrics import MetricsCollector
from deile_bot.foundation.output_formatter import PlainTextFormatter
from deile_bot.foundation.permissions import PermissionGate
from deile_bot.foundation.persona_selector import PersonaSelector
from deile_bot.foundation.pipeline import EgressPipeline, IngressPipeline
from deile_bot.foundation.rate_limit import RateLimiter
from deile_bot.foundation.settings import BotSettings, FoundationSettings
from deile_bot.tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def _build(store, classifier_name):
    settings = BotSettings(foundation=FoundationSettings(intent_classifier=classifier_name))
    adapter = FakeProviderAdapter()
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    bridge = CapturingFakeBridge()
    intent = build_intent_classifier(settings.foundation)
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl, store=store, audit=audit, event_bus=bus, metrics=metrics, dlq=dlq,
    )
    p = IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store,
        intent=intent, bridge=bridge, capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity), audit=audit,
        event_bus=bus, metrics=metrics, egress=egress, agent_meta=FakeAgentMetaProvider(),
    )
    return p, adapter, bridge


async def test_e2e7_heuristic_skips_noise_in_group(store):
    pipeline, adapter, bridge = await _build(store, "heuristic")
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.GROUP),
        text="random talk noise here",
    )
    await pipeline.handle(env, adapter)
    assert bridge.invocations == []


async def test_e2e7_always_respond_responds_to_anything(store):
    pipeline, adapter, bridge = await _build(store, "always_respond")
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.GROUP),
        text="qualquer coisa",
    )
    await pipeline.handle(env, adapter)
    assert len(bridge.invocations) == 1


async def test_e2e7_addressed_only_skips_unrelated_group(store):
    pipeline, adapter, bridge = await _build(store, "always_respond_to_addressed")
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.GROUP),
        text="conversa entre dois usuarios",
    )
    await pipeline.handle(env, adapter)
    assert bridge.invocations == []
