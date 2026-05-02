"""RateLimiter — token bucket per user + global concurrency semaphore."""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from deile_bot.foundation.envelope import BotUser, Channel
from deile_bot.foundation.exceptions import RateLimited
from deile_bot.foundation.settings import BotSettings


class TokenBucket:
    """Async-friendly token bucket."""

    def __init__(self, capacity: int, refill_per_minute: int):
        self.capacity = max(1, capacity)
        self.refill_per_second = max(0.0, refill_per_minute / 60.0)
        self._tokens: float = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def try_acquire(self, n: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.refill_per_second
            )
            self._last_refill = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False


class RateLimiter:
    """Per-user inbound + outbound rate limiting + global concurrency cap."""

    def __init__(self, settings: BotSettings):
        self._settings = settings
        f = settings.foundation
        self._burst = f.rate_limit_user_burst
        self._refill = f.rate_limit_user_refill_per_minute
        self._global = asyncio.Semaphore(f.rate_limit_global_concurrent)
        self._inbound_buckets: Dict[str, TokenBucket] = {}
        self._outbound_buckets: Dict[str, TokenBucket] = {}
        self._stats = {
            "user_burst": 0,
            "global_concurrent": 0,
            "outbound_burst": 0,
        }

    def _bucket_for(self, store: Dict[str, TokenBucket], user_id: str) -> TokenBucket:
        b = store.get(user_id)
        if b is None:
            b = TokenBucket(self._burst, self._refill)
            store[user_id] = b
        return b

    async def acquire_inbound(self, user: BotUser) -> None:
        bucket = self._bucket_for(self._inbound_buckets, user.bot_user_id)
        if not await bucket.try_acquire():
            self._stats["user_burst"] += 1
            raise RateLimited(
                "user inbound burst exceeded",
                context={"reason": "user_burst", "bot_user_id": user.bot_user_id},
            )
        # Global concurrency: try non-blocking acquire; if exhausted, raise.
        if self._global._value <= 0:  # type: ignore[attr-defined]
            self._stats["global_concurrent"] += 1
            raise RateLimited(
                "global concurrency exhausted",
                context={"reason": "global_concurrent"},
            )

    async def acquire_outbound(
        self,
        user: BotUser,
        channel: Optional[Channel] = None,
    ) -> None:
        bucket = self._bucket_for(self._outbound_buckets, user.bot_user_id)
        if not await bucket.try_acquire():
            self._stats["outbound_burst"] += 1
            raise RateLimited(
                "outbound burst exceeded",
                context={"reason": "outbound_burst", "bot_user_id": user.bot_user_id},
            )

    def stats(self) -> Dict[str, int]:
        return dict(self._stats)
