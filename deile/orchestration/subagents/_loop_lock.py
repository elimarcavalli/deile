"""Lazy-init de asyncio.Lock por event loop (compartilhado entre
SubAgentOrchestrator._get_capture_lock e
DispatchParallelSubagentsTool._get_locks_guard).

Resolve o problema clássico de criar asyncio.Lock em escopo de classe:
ele "binda" ao loop do primeiro __aenter__ e quebra com
RuntimeError em testes que rodam um loop por test ou no CLI que
chama asyncio.run() múltiplas vezes (e.g. _run_self_install +
_run_oneshot). A solução rastreia id(loop) corrente e recria o
Lock quando muda — sem inspecionar Lock._loop (API privada do CPython).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional


class LoopBoundLock:
    """Holder thread-friendly de um asyncio.Lock rebound por event loop.

    Use uma instância como ClassVar na classe consumidora; cada chamada
    a ``.get()`` retorna o Lock corrente para o event loop ativo,
    criando um novo se o loop mudou desde a última chamada.
    """

    __slots__ = ("_lock", "_loop_id", "_mutation_guard")

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None
        # Guard the read-modify-write of (_lock, _loop_id) against threaded
        # callers (pytest-asyncio in multithread, helpers que tocam a mesma
        # instância de dois loops). Coroutines no mesmo loop já são seguras
        # por cooperative scheduling — este guard cobre o caso cross-thread.
        self._mutation_guard = threading.Lock()

    def get(self) -> asyncio.Lock:
        """Retorna o Lock bindado ao event loop corrente.

        Se o loop mudou desde a última chamada, descarta a referência ao
        Lock anterior e cria um novo. **Caveat**: ao descartar o Lock antigo
        não verificamos se há awaiters pendentes — o cenário é improvável
        (loops descartados após ``asyncio.run()`` retornar tipicamente não
        têm coroutines vivas), mas se uma coroutine do loop antigo ficou
        bloqueada em ``async with``, ela ficaria com wakeup-perdido. Por
        isso este helper é destinado a singletons class-level cujo ciclo
        de vida coincide com o do loop owner.
        """
        try:
            loop = asyncio.get_running_loop()
            loop_id: Optional[int] = id(loop)
        except RuntimeError:  # pragma: no cover — só chamado de async
            loop_id = None
        with self._mutation_guard:
            needs_new = self._lock is None
            if (
                not needs_new
                and loop_id is not None
                and self._loop_id is not None
                and self._loop_id != loop_id
            ):
                needs_new = True
            if needs_new:
                self._lock = asyncio.Lock()
                self._loop_id = loop_id
            return self._lock  # type: ignore[return-value]

    def reset(self) -> None:
        """Force-reset (usado em testes que quiserem limpar o estado)."""
        with self._mutation_guard:
            self._lock = None
            self._loop_id = None
