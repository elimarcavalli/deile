"""E2E-9 — concurrency across multiple channels."""

from __future__ import annotations

import asyncio

import pytest

from deile_bot._testing import make_channel, make_envelope, make_user
from deile_bot.foundation.envelope import ChannelScope

pytestmark = pytest.mark.e2e


async def test_e2e9_concurrent_channels_no_deadlock(pipeline, fake_adapter, store):
    """5 channels x 5 envelopes simultaneous; all processed; no race."""
    tasks = []
    for ch_idx in range(5):
        ch = make_channel(
            provider="fake",
            provider_channel_id=f"c{ch_idx}",
            scope=ChannelScope.DM,
        )
        u = make_user(provider="fake", provider_user_id=f"u{ch_idx}")
        for i in range(5):
            env = make_envelope(
                channel=ch, author=u, text=f"msg {ch_idx}-{i}",
                message_id=f"m-{ch_idx}-{i}",
            )
            tasks.append(pipeline.handle(env, fake_adapter))
    await asyncio.gather(*tasks)
    # 25 inbound + 25 outbound; messages table reflects
    cur = await store._db.execute("SELECT COUNT(*) FROM message")
    row = await cur.fetchone()
    await cur.close()
    assert row[0] >= 50
