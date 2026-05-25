"""Stdio capture machinery for ``SubAgentOrchestrator`` (SRP extract).

Extraído de :mod:`deile.orchestration.subagents.orchestrator` (item 9 — SRP):
o orquestrador agregava três responsabilidades (coordenação paralela, redirect
de stdout/stderr e captura com cap). Esta camada isola a **captura** — um
``TextIO`` write-only com limite de tamanho (``CappedBuffer``), o accessor do
cap via :mod:`deile.config.settings` (Pilar 9 — configuração centralizada),
e o lock-holder que serializa dispatches concorrentes que mutam ``sys.stdout``.

Garantias preservadas:
  * ``CappedBuffer`` mantém os primeiros ``max_bytes`` caracteres; descarta o
    resto sem quebrar ``print()`` / ``subprocess`` line-buffering. Após o
    limite, escreve uma única marca ``[...truncated]`` na primeira tentativa
    pós-cap pra deixar claro pra debug que algo foi cortado.
  * ``max_bytes=None`` (default) resolve o limite *lazy* via
    :func:`get_capture_buffer_max_bytes` — respeita overrides em runtime
    (env ``DEILE_SUBAGENT_CAPTURE_BUFFER_MAX_BYTES`` / settings).
  * ``get_capture_lock()`` retorna um :class:`asyncio.Lock` lazy-bound ao
    event loop corrente (MA5 — iter-2). O singleton
    :data:`_capture_lock_holder` é exposto como ``SubAgentOrchestrator
    ._CAPTURE_LOCK_HOLDER`` para retrocompat com testes que chamam
    ``.reset()`` por ali.

Compat:
  * ``orchestrator._CappedBuffer`` permanece exportado como alias para
    :class:`CappedBuffer` (testes importam o nome privado histórico).
  * ``orchestrator._get_capture_buffer_max_bytes`` permanece exportado como
    alias para :func:`get_capture_buffer_max_bytes`.
  * ``SubAgentOrchestrator._CAPTURE_LOCK_HOLDER`` continua sendo a mesma
    instância de :class:`LoopBoundLock` exposta como
    :data:`_capture_lock_holder` aqui.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from deile.config.settings import get_settings

from ._loop_lock import LoopBoundLock


def get_capture_buffer_max_bytes() -> int:
    """Cap do buffer de captura — lido via :mod:`deile.config.settings`.

    Sub-DEILE pode rodar ``apt install`` ou ``npm install`` que despeja MB
    de output. Sem o cap, 5 sub-DEILEs em paralelo manteriam dezenas de MB
    em RAM até o resultado ser devolvido (e o ``data`` da tool é truncado
    em ``summary[:400]`` downstream — o buffer completo é desperdício).

    Default histórico: 256 KiB. Overridable via
    ``subagent_capture_buffer_max_bytes`` em settings (env
    ``DEILE_SUBAGENT_CAPTURE_BUFFER_MAX_BYTES`` ou
    ``~/.deile/settings.json``).
    """
    return int(getattr(get_settings(), "subagent_capture_buffer_max_bytes", 256 * 1024))


class CappedBuffer:
    """``TextIO`` write-only com limite de tamanho.

    Mantém os primeiros ``max_bytes`` caracteres; descarta o resto sem
    quebrar ``print()`` / ``subprocess`` line-buffering. Após o limite,
    escreve uma única marca ``[...truncated]`` na primeira tentativa pós-cap
    pra deixar claro pra debug que algo foi cortado.

    Issue #257 round 3 — substitui o ``StringIO`` unbounded original (C5).

    ``max_bytes=None`` (default) faz o limite ser resolvido lazy via
    :func:`get_capture_buffer_max_bytes`, respeitando overrides em runtime
    (env/settings).
    """

    __slots__ = ("_chunks", "_size", "_max", "_truncated")

    def __init__(self, max_bytes: Optional[int] = None) -> None:
        if max_bytes is None:
            max_bytes = get_capture_buffer_max_bytes()
        self._chunks: list = []
        self._size: int = 0
        self._max: int = max(0, int(max_bytes))
        self._truncated: bool = False

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        n = len(s)
        if self._size >= self._max:
            if not self._truncated:
                self._chunks.append("\n[...truncated]\n")
                self._truncated = True
            return n  # report success per file protocol
        # Espaço restante; pode ser tudo ou parte.
        remaining = self._max - self._size
        if n <= remaining:
            self._chunks.append(s)
            self._size += n
        else:
            self._chunks.append(s[:remaining])
            self._chunks.append("\n[...truncated]\n")
            self._size = self._max
            self._truncated = True
        return n

    def flush(self) -> None:
        return None

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def getvalue(self) -> str:
        """Retorna conteúdo agregado — compatível com ``io.StringIO.getvalue``."""
        return "".join(self._chunks)


# Singleton ``LoopBoundLock`` que serializa dispatches do orquestrador com
# ``capture_output=True``. Vive aqui (módulo de captura) em vez de attribute
# da classe ``SubAgentOrchestrator`` — a classe ainda expõe o atributo
# ``_CAPTURE_LOCK_HOLDER`` apontando para esta instância (retrocompat com
# testes que chamam ``SubAgentOrchestrator._CAPTURE_LOCK_HOLDER.reset()``).
_capture_lock_holder: LoopBoundLock = LoopBoundLock()


def get_capture_lock() -> asyncio.Lock:
    """Retorna o ``asyncio.Lock`` lazy-bound ao event loop corrente.

    Delega a :class:`LoopBoundLock`, que cria/troca o Lock conforme o
    loop muda — evitando ``RuntimeError: ... is bound to a different
    event loop`` em múltiplos ``asyncio.run()`` (CLI sub-comandos,
    pytest loop-per-test).
    """
    return _capture_lock_holder.get()


__all__ = [
    "CappedBuffer",
    "get_capture_buffer_max_bytes",
    "get_capture_lock",
    "_capture_lock_holder",
]
