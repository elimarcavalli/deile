"""Circuit breaker in-memory (3-state) para o cliente do deile-worker.

Protege o caller de martelar um worker degradado: depois de
``failure_threshold`` falhas consecutivas o circuito ABRE e rejeita os
dispatches seguintes sem tocar a rede (``CIRCUIT_OPEN``). Após
``reset_timeout_s`` segundos passa para HALF-OPEN e deixa UM probe passar;
sucesso fecha o circuito, falha o reabre (issue #620 AC5).

Estados (transições):

    closed  --(N falhas consecutivas)-->  open
    open    --(passados reset_timeout_s)-->  half-open  (apenas no probe)
    half-open --(probe OK)-->  closed
    half-open --(probe falha)-->  open

Limitação V1 (documentada na issue): o estado vive em memória — reinicia
como ``closed`` após restart do processo. Aceitável como proteção de
curto prazo; CB persistente (cross-restart) fica para #621.

A instância é compartilhada por todos os dispatches do processo, então o
estado é protegido por um :class:`asyncio.Lock`. ``time_source`` é
injetável para testar a janela de reset com fake clock sem ``sleep`` real.
"""

from __future__ import annotations

import asyncio
import time
from enum import IntEnum
from typing import Callable


class CircuitState(IntEnum):
    """Estados do breaker. Os valores inteiros são os expostos na métrica
    Prometheus ``deile_worker_circuit_breaker_state`` (0/1/2)."""

    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


class CircuitBreaker:
    """Breaker 3-state thread-safe (event-loop-safe via ``asyncio.Lock``).

    Args:
        failure_threshold: falhas consecutivas que abrem o circuito (default 5).
        reset_timeout_s: segundos em OPEN antes de permitir o probe HALF-OPEN.
        time_source: fonte monotônica injetável (default ``time.monotonic``).
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._now = time_source
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Estado corrente — leitura sem lock (eventualmente consistente),
        usada apenas para a métrica gauge."""
        return self._state

    def reset(self) -> None:
        """Volta ao estado inicial CLOSED. Síncrono — só para uso em
        testes/bootstrap, fora de um dispatch ativo."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0

    async def allow(self) -> bool:
        """Decide se o próximo dispatch pode prosseguir.

        Retorna ``True`` quando CLOSED, quando o probe HALF-OPEN é liberado
        (a janela de reset expirou) ou quando já estamos HALF-OPEN aguardando
        o resultado do probe. Retorna ``False`` enquanto OPEN dentro da
        janela — o caller deve falhar com ``CIRCUIT_OPEN`` sem I/O.
        """
        async with self._lock:
            if self._state is CircuitState.OPEN:
                if (self._now() - self._opened_at) >= self._reset_timeout_s:
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False
            return True

    async def record_success(self) -> None:
        """Sucesso: zera o contador e fecha o circuito (inclui o probe)."""
        async with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        """Falha: incrementa o contador; abre ao atingir o threshold.

        Uma falha em HALF-OPEN (probe) reabre imediatamente, independente do
        contador — o worker continua degradado.
        """
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = self._now()
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._now()
