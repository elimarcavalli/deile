"""Orquestrador de sub-DEILEs paralelos (issue #257).

``SubAgentOrchestrator.run`` recebe uma lista de :class:`SubAgentTask`, dispara
N runners em paralelo (``asyncio.create_task``) limitados por um semaphore,
aciona o renderer opcional e devolve a lista final de :class:`SubAgentState`.

Garantias:
  * Concorrência limitada por ``max_parallel`` (Semaphore).
  * Falhas isoladas: cada runner encapsula exceções e marca ``status="error"``;
    o orquestrador também drena ``task.exception()`` no fim (rede de segurança).
  * Budget global via ``asyncio.wait_for(timeout=_get_budget_s())``; em timeout
    cancelamos runners pendentes e marcamos como ``cancelled`` com erro
    ``subagent_budget_exceeded``.
  * ``sys.stdout``/``sys.stderr`` redirecionados para buffers capped durante a
    execução quando ``capture_output=True`` — evita que ``print()`` em tools
    polua o terminal. Painel mantém ref ao stdout REAL (capturado antes do
    redirect). Serializado por ``_CAPTURE_LOCK`` (lazy por event loop).
  * Cancel cooperativo: ESC pelo renderer ⇒ cancelamos runners; ``CancelledError``
    vindo do parent ⇒ cancelamos tudo e re-raise após cleanup (Pilar 03 §6).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, List, Literal, Optional, TextIO

from deile.config.settings import get_settings

from .events import SubAgentEvent, SubAgentState, SubAgentTask
from .runner import SubAgentRunner

logger = logging.getLogger(__name__)


def _get_budget_s() -> float:
    """Teto global de tempo (Pilar 9 — config centralizada via Settings)."""
    return float(getattr(get_settings(), "subagent_budget_s", 600.0))


def get_max_subagent_budget_s() -> float:
    """Public accessor — retorna o budget global em runtime, respeitando overrides
    posteriores ao import (env/settings)."""
    return _get_budget_s()


CancellationReason = Literal["user_esc", "budget_exceeded", "parent_cancel"]


@dataclass
class SubAgentResult:
    """Saída agregada de :meth:`SubAgentOrchestrator.run` — vai pro ToolResult."""

    states: List[SubAgentState]
    elapsed_s: float
    ok_count: int
    error_count: int
    cancelled: bool = False
    # Discrimina causa raiz do cancel (None quando cancelled=False).
    cancellation_reason: Optional[CancellationReason] = None
    captured_stdout: str = ""
    captured_stderr: str = ""

    _CANCEL_LABELS: ClassVar[dict] = {
        "user_esc": "cancelado pelo usuário (ESC)",
        "budget_exceeded": "cancelado por budget estourado",
        "parent_cancel": "cancelado pelo caller (parent)",
    }
    _CANCEL_LABELS_MD: ClassVar[dict] = {
        "user_esc": "ESC do usuário",
        "budget_exceeded": "budget estourado",
        "parent_cancel": "cancelado pelo caller",
    }

    @property
    def ok_global(self) -> bool:
        return self.error_count == 0 and not self.cancelled

    def consolidated_summary(self) -> str:
        """Resumo curto agregado para o LLM (≤2KB)."""
        lines: List[str] = []
        header = (
            f"sub-DEILEs paralelos · {self.ok_count} ok · "
            f"{self.error_count} erro · {self.elapsed_s:.1f}s total"
        )
        if self.cancelled:
            header += f" · {self._CANCEL_LABELS.get(self.cancellation_reason or '', 'cancelado')}"
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
        return "\n".join(lines)[:2000]

    def markdown_summary(self) -> str:
        """Versão markdown para gravar no histórico e replay via /resume."""
        status_emoji = "✅" if self.ok_global else ("⏹" if self.cancelled else "⚠️")
        header = (
            f"{status_emoji} **Sub-DEILEs paralelos** · "
            f"{self.ok_count} ok · {self.error_count} erro · "
            f"{self.elapsed_s:.1f}s total"
        )
        if self.cancelled and self.cancellation_reason:
            label = self._CANCEL_LABELS_MD.get(self.cancellation_reason, self.cancellation_reason)
            header += f" · _{label}_"
        lines: List[str] = [header, ""]
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
    """Fan-out síncrono de :class:`SubAgentEvent` para callbacks múltiplos."""

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


def _get_capture_buffer_max_bytes() -> int:
    """Lê o cap de captura via Settings (Pilar 9)."""
    return int(getattr(get_settings(), "subagent_capture_buffer_max_bytes", 256 * 1024))


class _CappedBuffer:
    """``TextIO`` write-only com limite de tamanho.

    Substitui ``StringIO`` unbounded — sub-DEILEs podem despejar MBs de output
    (``apt install`` etc.) e o consumidor só usa os primeiros KBs. Marca
    ``[...truncated]`` uma vez ao atingir o cap.

    ``max_bytes=None`` resolve lazy via :func:`_get_capture_buffer_max_bytes`
    (respeita overrides em runtime).
    """

    __slots__ = ("_chunks", "_size", "_max", "_truncated")

    def __init__(self, max_bytes: Optional[int] = None) -> None:
        if max_bytes is None:
            max_bytes = _get_capture_buffer_max_bytes()
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
            return n
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
        return "".join(self._chunks)


def _lazy_lock_for_loop(
    current_lock: Optional[asyncio.Lock],
    current_loop_id: Optional[int],
) -> tuple[asyncio.Lock, Optional[int]]:
    """Lazy-init/rebind de ``asyncio.Lock`` ao event loop corrente.

    Retorna ``(lock, loop_id)`` — cria novo lock se nenhum existe ou se o loop
    mudou (testes loop-per-test, múltiplos ``asyncio.run()``). Rastreamos
    ``id(loop)`` em vez de inspecionar ``Lock._loop`` (API privada do CPython).
    """
    try:
        loop_id: Optional[int] = id(asyncio.get_running_loop())
    except RuntimeError:  # pragma: no cover — só chamado de async
        loop_id = None
    if current_lock is None or (
        loop_id is not None
        and current_loop_id is not None
        and current_loop_id != loop_id
    ):
        return asyncio.Lock(), loop_id
    return current_lock, current_loop_id


class SubAgentOrchestrator:
    """Coordena disparo paralelo + (opcional) renderer multipanel.

    Quando ``capture_output=True``, redireciona ``sys.stdout``/``sys.stderr``
    para buffers durante a execução, serializado por :attr:`_CAPTURE_LOCK`
    (lazy-bound ao event loop corrente) — entradas concorrentes recebem
    ``RuntimeError`` em vez de bloquear (sub-DEILEs são raros e o caller tem
    cooldown de 5s).

    Args:
        runner: implementação :class:`SubAgentRunner`.
        max_parallel: teto de concorrência.
        renderer_factory: callable ``(states, broadcast, real_stdout) -> renderer``
            com método ``async run()``. Mantemos como factory para não acoplar
            esta camada à UI.
        capture_output: ``True`` redireciona stdout/stderr; ``False`` desabilita
            (útil em testes que querem ver output bruto).
    """

    _CAPTURE_LOCK: ClassVar[Optional[asyncio.Lock]] = None
    _CAPTURE_LOCK_LOOP_ID: ClassVar[Optional[int]] = None

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

    @classmethod
    def _get_capture_lock(cls) -> asyncio.Lock:
        cls._CAPTURE_LOCK, cls._CAPTURE_LOCK_LOOP_ID = _lazy_lock_for_loop(
            cls._CAPTURE_LOCK, cls._CAPTURE_LOCK_LOOP_ID
        )
        return cls._CAPTURE_LOCK

    async def run(self, tasks: List[SubAgentTask]) -> SubAgentResult:
        """Dispara ``tasks`` em paralelo e devolve o estado final agregado.

        Com ``capture_output=True``, fail-fast (não bloqueia) se outro dispatch
        já está rodando — concurrent stdout capture corromperia o stream.
        """
        if not tasks:
            return SubAgentResult(states=[], elapsed_s=0.0, ok_count=0, error_count=0)

        if not self._capture_output:
            return await self._run_locked(tasks)

        # asyncio é single-threaded cooperativo: entre ``locked()`` e o acquire
        # síncrono (não há await), nenhum sibling pode escapar pela janela.
        lock = self._get_capture_lock()
        if lock.locked():
            raise RuntimeError(
                "SubAgentOrchestrator.run(capture_output=True): another dispatch "
                "is already running with stdout capture; concurrent capture would "
                "corrupt sys.stdout state. Retry sequentially or pass "
                "capture_output=False."
            )
        await lock.acquire()
        try:
            return await self._run_locked(tasks)
        finally:
            try:
                lock.release()
            except RuntimeError:  # pragma: no cover — double-release guard
                pass

    async def _run_locked(self, tasks: List[SubAgentTask]) -> SubAgentResult:
        """Corpo do :meth:`run` — chamado já dentro do lock (quando aplicável)."""
        states = [SubAgentState(task=t) for t in tasks]
        broadcast = _Broadcast()
        start = time.monotonic()

        # Captura stdout REAL antes do redirect — painel escreve nele.
        real_stdout: TextIO = sys.stdout

        renderer = self._make_renderer(states, broadcast, real_stdout)
        semaphore = asyncio.Semaphore(self._max_parallel)

        async def _run_one(state: SubAgentState) -> None:
            async with semaphore:
                await self._runner.run_one(state, on_event=broadcast.emit)

        captured_out = _CappedBuffer() if self._capture_output else None
        captured_err = _CappedBuffer() if self._capture_output else None
        prev_stdout, prev_stderr = sys.stdout, sys.stderr
        if self._capture_output:
            sys.stdout = captured_out  # type: ignore[assignment]
            sys.stderr = captured_err  # type: ignore[assignment]

        runner_tasks: List[asyncio.Task] = []
        renderer_task: Optional[asyncio.Task] = None
        cancelled = False
        budget_exceeded = False
        outer_cancelled = False
        budget_s = _get_budget_s()
        try:
            runner_tasks = [asyncio.create_task(_run_one(st)) for st in states]
            if renderer is not None and hasattr(renderer, "run"):
                renderer_task = asyncio.create_task(renderer.run())

            try:
                await asyncio.wait_for(
                    self._wait_runners_and_renderer(runner_tasks, renderer_task, renderer),
                    timeout=budget_s,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                is_cancel = isinstance(exc, asyncio.CancelledError)
                pending_count = sum(1 for t in runner_tasks if not t.done())
                if is_cancel:
                    outer_cancelled = True
                    logger.info(
                        "SubAgentOrchestrator: caller cancelou; cancelando %d runner(s)",
                        pending_count,
                    )
                else:
                    budget_exceeded = True
                    logger.warning(
                        "SubAgentOrchestrator: budget de %.1fs estourado; cancelando %d runner(s)",
                        budget_s, pending_count,
                    )
                for t in runner_tasks:
                    if not t.done():
                        t.cancel()
                if renderer_task is not None and not renderer_task.done():
                    renderer_task.cancel()
                # Timeout curto p/ permitir cleanup dos runners; runners que ignoram
                # cancel (subprocess blocking) ficam órfãos em troca de progresso.
                await self._await_or_orphan(
                    asyncio.gather(*runner_tasks, return_exceptions=True),
                    timeout=5.0,
                    on_timeout=lambda: logger.error(
                        "SubAgentOrchestrator: %d runner task(s) ignored cancel after 5s",
                        sum(1 for t in runner_tasks if not t.done()),
                    ),
                )
                if renderer_task is not None:
                    await self._await_or_orphan(
                        asyncio.gather(renderer_task, return_exceptions=True),
                        timeout=2.0,
                        on_timeout=lambda: logger.warning(
                            "SubAgentOrchestrator: renderer ignored cancel after 2s"
                        ),
                    )
                # Marca states pendentes como cancelled (budget path apenas — no
                # parent_cancel, re-raise abaixo descarta o result).
                if not is_cancel:
                    for st in states:
                        if not st.is_terminal:
                            st.status = "cancelled"
                            st.error = "subagent_budget_exceeded"
                            if st.finished_at is None:
                                st.finished_at = time.monotonic()

            # Garante que renderer realmente terminou antes do finally restaurar
            # stdout. Distingue parent-cancel (re-raise) vs our-cancel (engolir).
            if renderer_task is not None and not renderer_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(renderer_task), timeout=1.5)
                except asyncio.TimeoutError:
                    renderer_task.cancel()
                    try:
                        await renderer_task
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        parent_cancelling = (
                            current is not None
                            and getattr(current, "cancelling", lambda: 0)() > 0
                        )
                        if parent_cancelling and not renderer_task.cancelled():
                            raise
                    except Exception:  # noqa: BLE001 — renderer can't break orchestrator
                        logger.warning("renderer raised during shutdown", exc_info=True)

            if renderer is not None and getattr(renderer, "cancelled", False):
                cancelled = True

            # Drena exceções dos runners (não devem propagar — runners encapsulam).
            for t in runner_tasks:
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        logger.error("runner task raised: %s", exc, exc_info=exc)
        finally:
            if self._capture_output:
                sys.stdout = prev_stdout
                sys.stderr = prev_stderr

        # Re-raise pós-cleanup (Pilar 03 §6 — CancelledError nunca é capturada
        # sem re-raise).
        if outer_cancelled:
            raise asyncio.CancelledError()

        elapsed = time.monotonic() - start
        ok_count = sum(1 for s in states if s.status == "ok")
        error_count = sum(1 for s in states if s.status in ("error", "cancelled"))
        # budget_exceeded tem precedência sobre user_esc (budget travado pode
        # ter induzido o ESC). parent_cancel nunca chega aqui — re-raise acima.
        cancellation_reason: Optional[CancellationReason] = None
        if budget_exceeded:
            cancellation_reason = "budget_exceeded"
        elif cancelled:
            cancellation_reason = "user_esc"
        return SubAgentResult(
            states=states,
            elapsed_s=elapsed,
            ok_count=ok_count,
            error_count=error_count,
            cancelled=cancelled or budget_exceeded,
            cancellation_reason=cancellation_reason,
            captured_stdout=(captured_out.getvalue() if captured_out else ""),
            captured_stderr=(captured_err.getvalue() if captured_err else ""),
        )

    def _make_renderer(self, states, broadcast, real_stdout) -> Optional[Any]:
        """Factory adapter — tolera assinaturas legadas ``(states, broadcast)``."""
        if self._renderer_factory is None:
            return None
        try:
            return self._renderer_factory(states, broadcast, real_stdout)
        except TypeError:
            return self._renderer_factory(states, broadcast)

    @staticmethod
    async def _await_or_orphan(awaitable, *, timeout: float, on_timeout) -> None:
        """Aguarda ``awaitable`` com timeout; chama ``on_timeout`` se estourar.

        Python 3.11+ aliases ``asyncio.TimeoutError`` ao built-in ``TimeoutError``;
        capturamos ambos para compat com 3.9-3.10.
        """
        try:
            await asyncio.wait_for(awaitable, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            on_timeout()

    async def _wait_runners_and_renderer(
        self,
        runner_tasks: List[asyncio.Task],
        renderer_task: Optional[asyncio.Task],
        renderer: Optional[Any],
    ) -> None:
        """Espera todos os runners + (eventualmente) o renderer.

        Extraído para que possa ser envolvido por ``asyncio.wait_for(budget)`` —
        timeout propaga e o caller cancela tudo.
        """
        pending: set = set(runner_tasks)
        if renderer_task is not None:
            pending.add(renderer_task)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            if renderer_task is not None and renderer_task in done:
                rt_cancelled = getattr(renderer, "cancelled", False) if renderer else False
                if rt_cancelled and not all(t.done() for t in runner_tasks):
                    for t in runner_tasks:
                        if not t.done():
                            t.cancel()
            if all(t.done() for t in runner_tasks) and renderer_task is not None:
                if not renderer_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(renderer_task), timeout=1.5
                        )
                    except asyncio.TimeoutError:
                        renderer_task.cancel()
                    pending.discard(renderer_task)


__all__ = [
    "SubAgentOrchestrator",
    "SubAgentResult",
    "get_max_subagent_budget_s",
]
