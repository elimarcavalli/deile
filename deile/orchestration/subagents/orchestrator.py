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
  * **Stdout/stderr redirect** (#257 round 2): durante a execução, ``sys.stdout``
    e ``sys.stderr`` apontam para buffers, para que ``print()`` em ferramentas
    sub-DEILE (notavelmente ``bash_tool``) não poluam o terminal do usuário.
    O painel ainda escreve no terminal porque seu :class:`Console` foi
    construído com ``file=_real_stdout`` (captura ANTES do redirect).
  * **Cancel-propagation**: se o painel sinaliza cancel (ESC em vista compacta),
    cancelamos os runner tasks órfãos antes de retornar — evita que prints
    posteriores cheguem ao terminal já restaurado.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, TextIO

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
    cancelled: bool = False
    # Buffer agregado do stdout/stderr capturado durante a execução. Útil para
    # diagnóstico em testes e potencialmente para um modo de "ver logs brutos"
    # no painel focado (próxima iteração).
    captured_stdout: str = ""
    captured_stderr: str = ""

    @property
    def ok_global(self) -> bool:
        return self.error_count == 0 and not self.cancelled

    def consolidated_summary(self) -> str:
        """Resumo curto agregado para o LLM (≤2KB).

        Cada frente vira ~2 linhas: status + descrição + arquivos. NÃO inclui o
        ``result_text`` completo — o LLM já viu o painel ao vivo e deve apenas
        consolidar; despejar resultados longos satura o contexto e induz o LLM
        a re-narrar (anti-padrão da issue #257).
        """
        lines: List[str] = []
        header = (
            f"sub-DEILEs paralelos · {self.ok_count} ok · "
            f"{self.error_count} erro · {self.elapsed_s:.1f}s total"
        )
        if self.cancelled:
            header += " · cancelado pelo usuário"
        lines.append(header)
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

    def markdown_summary(self) -> str:
        """Versão markdown do resumo para gravar no histórico e replay.

        Diferente do ``consolidated_summary``, este é renderizado num bloco
        Markdown (a CLI replay usa ``ui.display_response`` que parseia
        Markdown), então deve usar formatação rica e legível.
        """
        lines: List[str] = []
        status_emoji = "✅" if self.ok_global else ("⏹" if self.cancelled else "⚠️")
        lines.append(
            f"{status_emoji} **Sub-DEILEs paralelos** · "
            f"{self.ok_count} ok · {self.error_count} erro · "
            f"{self.elapsed_s:.1f}s total"
        )
        lines.append("")
        for st in self.states:
            glyph = {"ok": "✅", "error": "❌", "cancelled": "⏹"}.get(st.status, "•")
            line = f"- {glyph} **#{st.task.index} {st.task.description}**"
            if st.elapsed_s:
                line += f" _({st.elapsed_s:.1f}s)_"
            lines.append(line)
            if st.files_touched:
                files = ", ".join(f"`{f}`" for f in st.files_touched[:5])
                if len(st.files_touched) > 5:
                    files += f" _(+{len(st.files_touched) - 5})_"
                lines.append(f"  - Arquivos: {files}")
            if st.error:
                lines.append(f"  - Erro: `{st.error[:200]}`")
        return "\n".join(lines)


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


# Cap do buffer de stdout/stderr capturados — sub-DEILE pode rodar
# ``apt install`` ou ``npm install`` que despeja MB de output. Sem o cap,
# 5 sub-DEILEs em paralelo manteriam dezenas de MB em RAM até o resultado
# ser devolvido (e o ``data`` da tool é truncado em ``summary[:400]``
# downstream — o buffer completo é desperdício). Cap por stream.
_CAPTURE_BUFFER_MAX_BYTES: int = 256 * 1024  # 256 KiB cada stream


class _CappedBuffer:
    """``TextIO`` write-only com limite de tamanho.

    Mantém os primeiros ``max_bytes`` caracteres; descarta o resto sem
    quebrar ``print()`` / ``subprocess`` line-buffering. Após o limite,
    escreve uma única marca ``[...truncated]`` na primeira tentativa pós-cap
    pra deixar claro pra debug que algo foi cortado.

    Issue #257 round 3 — substitui o ``StringIO`` unbounded original (C5).
    """

    __slots__ = ("_chunks", "_size", "_max", "_truncated")

    def __init__(self, max_bytes: int = _CAPTURE_BUFFER_MAX_BYTES) -> None:
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


class SubAgentOrchestrator:
    """Coordena disparo paralelo + (opcional) renderer multipanel.

    Args:
        runner: implementação :class:`SubAgentRunner` que vai executar cada
            sub-tarefa. Veja :func:`resolve_runner`.
        max_parallel: teto de concorrência (default vem de settings).
        renderer_factory: callable opcional ``(states, broadcast, real_stdout) ->
            object`` com método ``async run()``. O orquestrador chama
            ``renderer.run()`` em paralelo aos runners e o renderer encerra
            sozinho quando todos os ``states`` atingem terminal. Mantemos como
            factory para evitar que esta camada conheça o tipo concreto (UI
            vive em ``deile/ui``).
        capture_output: quando ``True`` (default), redireciona
            ``sys.stdout``/``sys.stderr`` para buffers durante a execução —
            evita que ``print()`` em ferramentas como ``bash_tool`` polua o
            terminal do usuário. ``False`` desabilita o redirect (útil em
            testes que querem ver output bruto).
    """

    def __init__(
        self,
        runner: SubAgentRunner,
        *,
        max_parallel: int = 3,
        renderer_factory: Optional[Callable[..., Any]] = None,
        capture_output: bool = True,
    ) -> None:
        self._runner = runner
        self._max_parallel = max(1, int(max_parallel))
        self._renderer_factory = renderer_factory
        self._capture_output = bool(capture_output)

    async def run(self, tasks: List[SubAgentTask]) -> SubAgentResult:
        """Dispara ``tasks`` em paralelo e devolve o estado final agregado."""
        if not tasks:
            return SubAgentResult(states=[], elapsed_s=0.0, ok_count=0, error_count=0)

        states = [SubAgentState(task=t) for t in tasks]
        broadcast = _Broadcast()
        start = time.monotonic()

        # Capturamos referências aos streams REAIS antes de qualquer redirect.
        # O renderer (criado a seguir) recebe ``real_stdout`` e constrói seu
        # próprio :class:`rich.console.Console` ligado a esse handle — assim
        # o painel continua aparecendo no terminal mesmo enquanto ``sys.stdout``
        # está redirecionado para suprimir os ``print()`` dos sub-DEILEs.
        real_stdout: TextIO = sys.stdout
        real_stderr: TextIO = sys.stderr

        # Renderer é opcional: tests + chamadas headless passam None.
        renderer = None
        if self._renderer_factory is not None:
            try:
                renderer = self._renderer_factory(states, broadcast, real_stdout)
            except TypeError:
                # Backward-compat: factories antigas só aceitavam (states, bcast).
                renderer = self._renderer_factory(states, broadcast)

        semaphore = asyncio.Semaphore(self._max_parallel)

        async def _run_one(state: SubAgentState) -> None:
            async with semaphore:
                await self._runner.run_one(state, on_event=broadcast.emit)

        # ── Stdout/stderr redirect (issue #257 round 2, fix #1) ──────────────
        # Tools como ``bash_tool`` (linhas 129/180/182) chamam ``print()``
        # diretamente, e isso polui o terminal do usuário em sub-DEILEs locais.
        # Redirecionamos sys.stdout/sys.stderr para buffers durante a execução.
        # O painel mantém referência ao stdout REAL, então continua renderizando.
        captured_out = _CappedBuffer() if self._capture_output else None
        captured_err = _CappedBuffer() if self._capture_output else None
        prev_stdout = sys.stdout
        prev_stderr = sys.stderr
        if self._capture_output:
            sys.stdout = captured_out  # type: ignore[assignment]
            sys.stderr = captured_err  # type: ignore[assignment]

        runner_tasks: List[asyncio.Task] = []
        renderer_task: Optional[asyncio.Task] = None
        cancelled = False
        try:
            # Padrão idêntico ao pipeline stages.py:1050 — return_exceptions=True
            # para que uma falha não cancele siblings.
            runner_tasks = [asyncio.create_task(_run_one(st)) for st in states]
            if renderer is not None and hasattr(renderer, "run"):
                renderer_task = asyncio.create_task(renderer.run())

            # Cooperative cancel: se o renderer marca ``cancelled``, paramos
            # os runners cooperativamente para evitar prints residuais após o
            # restore de stdout. Aguardamos tanto os runners quanto o renderer.
            pending: set = set(runner_tasks)
            if renderer_task is not None:
                pending.add(renderer_task)

            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                # Se o renderer terminou primeiro com cancel, propaga.
                if renderer_task is not None and renderer_task in done:
                    rt_cancelled = getattr(renderer, "cancelled", False)
                    if rt_cancelled and not all(t.done() for t in runner_tasks):
                        cancelled = True
                        for t in runner_tasks:
                            if not t.done():
                                t.cancel()
                # Se todos os runners terminaram, fechamos o renderer.
                if all(t.done() for t in runner_tasks) and renderer_task is not None:
                    if not renderer_task.done():
                        # O renderer detecta states terminais e fecha sozinho;
                        # damos um tick a mais e cancelamos se travou.
                        try:
                            await asyncio.wait_for(asyncio.shield(renderer_task), timeout=1.5)
                        except asyncio.TimeoutError:
                            renderer_task.cancel()
                        pending.discard(renderer_task)
                # Se ainda há runners pendentes e renderer já fechou:
                # continuamos aguardando-os (mantém pending no while).

            # Drena exceções dos runners (não devem propagar — runners encapsulam).
            for t in runner_tasks:
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        logger.error("runner task raised: %s", exc, exc_info=exc)
        finally:
            # Restore stdout/stderr ANTES de qualquer print pós-execução.
            if self._capture_output:
                sys.stdout = prev_stdout
                sys.stderr = prev_stderr

        elapsed = time.monotonic() - start
        ok_count = sum(1 for s in states if s.status == "ok")
        error_count = sum(1 for s in states if s.status in ("error", "cancelled"))
        return SubAgentResult(
            states=states,
            elapsed_s=elapsed,
            ok_count=ok_count,
            error_count=error_count,
            cancelled=cancelled,
            captured_stdout=(captured_out.getvalue() if captured_out else ""),
            captured_stderr=(captured_err.getvalue() if captured_err else ""),
        )


__all__ = [
    "MAX_SUBAGENT_BUDGET_S",
    "SubAgentOrchestrator",
    "SubAgentResult",
]
