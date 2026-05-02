"""E2E-1 — happy path with FakeBridge (no API cost)."""

from __future__ import annotations

import pytest

from deile_bot._testing import make_channel, make_envelope, make_user
from deile_bot.foundation.envelope import ChannelScope

pytestmark = pytest.mark.e2e


async def test_e2e1_happy_path_fake_bridge(pipeline, fake_adapter, fake_bridge, store):
    """Inject inbound -> pipeline ingests -> bridge invoked -> outbound delivered."""
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.DM),
        author=make_user(provider="fake"),
        text="Olá DEILE, lista 3 fatos sobre Python",
    )
    await pipeline.handle(env, fake_adapter)
    assert len(fake_adapter.inbox) == 1
    assert len(fake_adapter.inbox[-1]["text"]) >= 50
    assert len(fake_bridge.invocations) == 1

    audit_types = {r["event_type"] for r in await store.query_audit()}
    assert {"inbound_received", "agent_invoked", "agent_responded", "outbound_sent"} <= audit_types
