"""worker_rate_limit — token bucket por canal do deile-worker (issue #620 AC7).

Controle de admissão por ``channel_id`` para evitar que um único canal
monopolize o worker. Cada canal tem um balde de tokens com ``capacity`` tokens
que recarrega a ``rate`` tokens/segundo. Um dispatch consome 1 token; sem
token disponível → rejeição com ``Retry-After`` (segundos até o próximo token).

Isolamento: cada canal tem seu próprio balde — um canal saturado não afeta os
demais. Limpeza lazy: um balde sem uso há mais de ``idle_reset_s`` segundos é
resetado para a capacidade cheia no próximo acesso (canais inativos não
acumulam dívida nem ocupam dívida histórica).

In-memory por processo (V1). Distributed rate limiting (Redis) → #621.
``time_source`` é injetável para teste determinístico.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Tuple


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """Token bucket in-memory por ator (``channel_id``), thread-safe.

    Args:
        capacity: tokens máximos no balde (burst permitido). Default 10.
        rate: tokens recarregados por segundo. Default 1/s.
        idle_reset_s: balde ocioso por mais que isto é resetado para cheio
            no próximo acesso. Default 300s.
        time_source: fonte monotônica injetável (default ``time.monotonic``).
    """

    def __init__(
        self,
        *,
        capacity: float = 10.0,
        rate: float = 1.0,
        idle_reset_s: float = 300.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._capacity = float(capacity)
        self._rate = float(rate)
        self._idle_reset_s = float(idle_reset_s)
        self._now = time_source
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def acquire(self, actor: str) -> Tuple[bool, float]:
        """Tenta consumir 1 token de *actor*.

        Returns:
            ``(allowed, retry_after_s)`` — ``allowed=True`` quando havia token
            (consumido); ``retry_after_s`` é 0.0 quando permitido, ou os
            segundos até o próximo token quando rejeitado.
        """
        now = self._now()
        with self._lock:
            bucket = self._buckets.get(actor)
            if bucket is None or (now - bucket.last_refill) >= self._idle_reset_s:
                # Novo canal, ou ocioso o suficiente para resetar para cheio.
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[actor] = bucket
            else:
                elapsed = now - bucket.last_refill
                bucket.tokens = min(
                    self._capacity, bucket.tokens + elapsed * self._rate,
                )
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            # Sem token: tempo até o balde ter 1 token de novo.
            deficit = 1.0 - bucket.tokens
            retry_after = math.ceil(deficit / self._rate)
            return False, float(retry_after)
