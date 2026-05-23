"""Orquestrador de sub-DEILEs paralelos (issue #257).

``SubAgentOrchestrator.run`` recebe uma lista de :class:`SubAgentTask`, dispara
N runners em paralelo via ``asyncio.gather(return_exceptions=True)`` (padrão de
``deile/orchestration/pipeline/stages.py:1050``), aciona o renderer multipanel
(opcional) e devolve a lista final de :class:`SubAgentState`.

Garantias:
  * Concorrência limitada por :class:`asyncio.Semaphore` (``max_parallel``).
  * Falha de uma frente NÃO cancela siblings (``return_exceptions=True`` é a
    rede de segurança; os runners já encapsulam exceções e marcam ``status=
    "error"``).
  * ``run`` é idempotente: chama o renderer só se foi passado um.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .events import SubAgentEvent, SubAgentState, SubAgentTask
from .runner import OnEvent, SubAgentRunner

logger = logging.getLogger(__name__)


# Teto global de tempo da invocação do tool. Override via
# ``DEILE_SUBAGENT_BUDGET_S``; default = 10min (mesmo budget do worker).
import os
MAX_SUBAGENT_BUDGET_S: float = float(
    os.environ.get("DEILE_SUBAGENT_BUDGET_S", "600")
)


@dataclass
class SubAgentResult:
    """Saída agregada de :meth:`SubAgentOrchestrator.run` — vai pro ToolResult."""

    states: List[SubAgentState]
    elapsed_s: float
    ok_count: int
    error_count: int

    @property
    def ok_global(self) -> bool:
        return self.error_count == 0

    def consolidated_summary(self) -> str:
        """Resumo curto agregado para o LLM (≤2KB).

        Cada frente vira ~2 linhas: status + descrição + arquivos. NÃO inclui o
        ``result_text`` completo — o LLM já viu o painel ao vivo e deve apenas
        consolidar; despejar resultados longos satura o contexto e induz o LLM
        a re-narrar (anti-padrão da issue #257).
        """
        lines: List[str] = []
        lines.append(
            f"sub-DEILEs paralelos · {self.ok_count} ok · "
            f"{self.error_count} erro · {self.elapsed_s:.1f}s total"
        )
        for st in self.states:
            glyph = {"ok": "✅", "error": "❌", "cancelled": "⏹"}.get(st.status, "•")
            files = ", ".join(st.files_touched[:5])
            if len(st.files_touched) > 5:
                files += f" (+{len(st.files_touched) - 5})"
            head = f"  #{st.task.index} {glyph} {st.task.description}"
            if files:
                head += f" — {files}"
            if st.elapsed_s:
                head += f" · {st.elapsed_s:.1f}s"
            lines.append(head)
            if st.error:
                lines.append(f"      erro: {st.error[:120]}")
        full = "\n".join(lines)
        return full[:2000]


class _Broadcast:
    """Fan-out de :class:`SubAgentEvent` para callbacks múltiplos (sync).

    Mantemos uma lista de subscribers e despachamos sincronamente: o renderer
    apenas atualiza um snapshot e a próxima frame do Live cuida do redraw.
    """

    def __init__(self) -> None:
        self._subs: List[Callable[[SubAgentEvent], None]] = []

    def subscribe(self, cb: Callable[[SubAgentEvent], None]) -> None:
        self._subs.append(cb)

    def emit(self, evt: SubAgentEvent) -> None:
        for cb in self._subs:
            try:
                cb(evt)
            except Exception:  # noqa: BLE001 — never break runner on UI bugs
                logger.exception("SubAgentEvent subscriber raised")


class SubAgentOrchestrator:
    """Coordena disparo paralelo + (opcional) renderer multipanel.

    Args:
        runner: implementação :class:`SubAgentRunner` que vai executar cada
            sub-tarefa. Veja :func:`resolve_runner`.
        max_parallel: teto de concorrência (default vem de settings).
        renderer_factory: callable opcional ``(states) -> object`` com método
            ``async run()``. O orquestrador chama ``renderer.run()`` em
            paralelo aos runners e o renderer encerra sozinho quando todos
            os ``states`` atingem terminal. Mantemos como factory para evitar
            que esta camada conheça o tipo concreto (UI vive em ``deile/ui``).
    """

    def __init__(
        self,
        runner: SubAgentRunner,
        *,
        max_parallel: int = 3,
        renderer_factory: Optional[Callable[[List[SubAgentState], _Broadcast], Any]] = None,
    ) -> None:
        self._runner = runner
        self._max_parallel = max(1, int(max_parallel))
        self._renderer_factory = renderer_factory

    async def run(self, tasks: List[SubAgentTask]) -> SubAgentResult:
        """Dispara ``tasks`` em paralelo e devolve o estado final agregado."""
        if not tasks:
            return SubAgentResult(states=[], elapsed_s=0.0, ok_count=0, error_count=0)

        states = [SubAgentState(task=t) for t in tasks]
        broadcast = _Broadcast()
        start = time.monotonic()

        # Renderer é opcional: tests + chamadas headless passam None.
        renderer = (
            self._renderer_factory(states, broadcast)
            if self._renderer_factory is not None
            else None
        )

        semaphore = asyncio.Semaphore(self._max_parallel)

        async def _run_one(state: SubAgentState) -> None:
            async with semaphore:
                await self._runner.run_one(state, on_event=broadcast.emit)

        # Padrão idêntico ao pipeline stages.py:1050 — return_exceptions=True
        # para que uma falha não cancele siblings.
        runner_tasks = [asyncio.create_task(_run_one(st)) for st in states]

        if renderer is not None and hasattr(renderer, "run"):
            renderer_task = asyncio.create_task(renderer.run())
        else:
            renderer_task = None

        try:
            await asyncio.gather(*runner_tasks, return_exceptions=True)
        finally:
            if renderer_task is not None:
                # Sinaliza ao renderer que todos os runners terminaram —
                # ele detecta via state.is_terminal e encerra seu Live.
                try:
                    await asyncio.wait_for(renderer_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    renderer_task.cancel()
                    try:
                        await renderer_task
                    except (asyncio.CancelledError, Exception):
                        pass

        elapsed = time.monotonic() - start
        ok_count = sum(1 for s in states if s.status == "ok")
        error_count = sum(1 for s in states if s.status in ("error", "cancelled"))
        return SubAgentResult(
            states=states,
            elapsed_s=elapsed,
            ok_count=ok_count,
            error_count=error_count,
        )


__all__ = [
    "MAX_SUBAGENT_BUDGET_S",
    "SubAgentOrchestrator",
    "SubAgentResult",
]
