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
from typing import Optional


class LoopBoundLock:
    """Holder thread-friendly de um asyncio.Lock rebound por event loop.

    Use uma instância como ClassVar na classe consumidora; cada chamada
    a ``.get()`` retorna o Lock corrente para o event loop ativo,
    criando um novo se o loop mudou desde a última chamada.
    """

    __slots__ = ("_lock", "_loop_id")

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None

    def get(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
            loop_id: Optional[int] = id(loop)
        except RuntimeError:  # pragma: no cover — só chamado de async
            loop_id = None
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
        self._lock = None
        self._loop_id = None
