"""E2E-6 — persona routing per (provider, scope, owner, channel_name)."""

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
from deile_bot.foundation.settings import (BotSettings, PermissionsSettings,
                                           PersonaRule, PersonaSettings)
from deile_bot.tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def test_e2e6_persona_routing_dm_owner_vs_group(store):
    """Owner in DM Discord -> developer; same owner in #geral -> host."""
    owner = make_user(bot_user_id="OWNER", provider="discord", provider_user_id="42")
    await store.upsert_user(owner)
    settings = BotSettings(
        permissions=PermissionsSettings(owners=["OWNER"]),
        personas=PersonaSettings(
            default="default",
            rules=[
                PersonaRule(
                    when={"provider": "discord", "scope": "DM", "owner": True},
                    use="developer",
                ),
                PersonaRule(
                    when={"provider": "discord", "scope": "GROUP", "channel_name_in": ["geral"]},
                    use="host",
                ),
            ],
        ),
    )
    adapter = FakeProviderAdapter()
    adapter.name = "discord"  # type: ignore[assignment]
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
    # DM (full text to clear "too_short" heuristic)
    env_dm = make_envelope(
        channel=make_channel(
            provider="discord", provider_channel_id="dm-X", scope=ChannelScope.DM
        ),
        author=owner,
        text="oi privado, conversa direta",
        message_id="m-dm-1",
    )
    await pipeline.handle(env_dm, adapter)
    # #geral — owner mentions self_user_id so heuristic responds
    env_g = make_envelope(
        channel=make_channel(
            provider="discord",
            provider_channel_id="geral-1",
            scope=ChannelScope.GROUP,
            name="geral",
        ),
        author=owner,
        text="oi DEILE",
        mentions=(make_user(provider_user_id=adapter.self_user_id, display_name="DEILE", is_bot=True),),
        message_id="m-g-1",
    )
    await pipeline.handle(env_g, adapter)
    assert len(bridge.invocations) == 2
    assert bridge.invocations[0].persona == "developer"
    assert bridge.invocations[1].persona == "host"
