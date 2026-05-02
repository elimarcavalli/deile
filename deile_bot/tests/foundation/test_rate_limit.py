"""Tests for RateLimiter."""

from __future__ import annotations

import asyncio

import pytest

from deile_bot._testing import make_user
from deile_bot.foundation.exceptions import RateLimited
from deile_bot.foundation.rate_limit import RateLimiter, TokenBucket
from deile_bot.foundation.settings import BotSettings, FoundationSettings


def _settings(burst=3, refill=60, concurrent=8) -> BotSettings:
    return BotSettings(
        foundation=FoundationSettings(
            rate_limit_user_burst=burst,
            rate_limit_user_refill_per_minute=refill,
            rate_limit_global_concurrent=concurrent,
        )
    )


class TestTokenBucket:
    async def test_acquire_until_empty(self):
        b = TokenBucket(capacity=3, refill_per_minute=0)
        for _ in range(3):
            assert await b.try_acquire()
        assert not await b.try_acquire()

    async def test_refill(self):
        b = TokenBucket(capacity=2, refill_per_minute=6000)  # 100/sec
        await b.try_acquire()
        await b.try_acquire()
        await asyncio.sleep(0.05)  # > 5 tokens refilled
        assert await b.try_acquire()


class TestRateLimiter:
    async def test_burst_then_block(self):
        rl = RateLimiter(_settings(burst=3))
        u = make_user()
        for _ in range(3):
            await rl.acquire_inbound(u)
        with pytest.raises(RateLimited) as excinfo:
            await rl.acquire_inbound(u)
        assert excinfo.value.context["reason"] == "user_burst"

    async def test_distinct_users_independent(self):
        rl = RateLimiter(_settings(burst=2))
        a = make_user(bot_user_id="A", provider_user_id="a")
        b = make_user(bot_user_id="B", provider_user_id="b")
        await rl.acquire_inbound(a)
        await rl.acquire_inbound(a)
        await rl.acquire_inbound(b)
        with pytest.raises(RateLimited):
            await rl.acquire_inbound(a)
        await rl.acquire_inbound(b)

    async def test_outbound_separate_bucket(self):
        rl = RateLimiter(_settings(burst=2))
        u = make_user()
        # Inbound bucket separate from outbound
        await rl.acquire_inbound(u)
        await rl.acquire_inbound(u)
        await rl.acquire_outbound(u)
        await rl.acquire_outbound(u)
        with pytest.raises(RateLimited):
            await rl.acquire_outbound(u)

    async def test_stats_increments(self):
        rl = RateLimiter(_settings(burst=1))
        u = make_user()
        await rl.acquire_inbound(u)
        try:
            await rl.acquire_inbound(u)
        except RateLimited:
            pass
        assert rl.stats()["user_burst"] >= 1
