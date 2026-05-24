"""Orquestrador de sub-DEILEs paralelos (issue #257).

``SubAgentOrchestrator.run`` recebe uma lista de :class:`SubAgentTask`, dispara
N runners em paralelo via ``asyncio.create_task`` + ``asyncio.wait`` (com
``return_when=FIRST_COMPLETED`` para coordenar com o renderer multipanel),
aciona o renderer (opcional) e devolve a lista final de :class:`SubAgentState`.

Garantias:
  * Concorrência limitada por :class:`asyncio.Semaphore` (``max_parallel``).
  * Falha de uma frente NÃO cancela siblings (cada runner encapsula exceções
    e marca ``status="error"``; o orquestrador drena ``task.exception()`` no
    final como rede de segurança).
  * **Budget global** (M2/M11 — issue #295 review): a execução inteira é
    envolvida por ``asyncio.wait_for(timeout=subagent_budget_s)``; em
    TimeoutError marcamos states ainda pendentes como ``cancelled`` com
    erro ``subagent_budget_exceeded``.
  * **Stdout/stderr redirect** (#257 round 2): durante a execução, ``sys.stdout``
    e ``sys.stderr`` apontam para buffers, para que ``print()`` em ferramentas
    sub-DEILE (notavelmente ``bash_tool``) não poluam o terminal do usuário.
    O painel ainda escreve no terminal porque seu :class:`Console` foi
    construído com ``file=_real_stdout`` (captura ANTES do redirect).
    Serializado por um :class:`asyncio.Lock` lazy-bound ao event loop corrente
    (MA5 — iter-2 review): como ``sys.stdout`` é estado global do processo,
    dispatches concorrentes com ``capture_output=True`` corromperiam streams
    uns dos outros. ``_run()`` tenta ``acquire_nowait()`` atomicamente — quem
    chega com o lock ocupado recebe ``RuntimeError`` em vez de bloquear
    (sub-DEILEs são raros e o caller já tem cooldown de 5s).
  * **Cancel-propagation**: se o painel sinaliza cancel (ESC em vista compacta),
    cancelamos os runner tasks órfãos antes de retornar — evita que prints
    posteriores cheguem ao terminal já restaurado. Para cancel injetado pelo
    parent (CancelledError vindo do caller de ``run()``), também cancelamos
    runners + renderer e re-raise (Pilar 03 §6) — MA3 (iter-2 review).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, List, Literal, Optional, TextIO

from deile.config.settings import get_settings

from ._capture import (CappedBuffer, _capture_lock_holder,
                       get_capture_buffer_max_bytes, get_capture_lock)
from ._loop_lock import LoopBoundLock
from .events import SubAgentEvent, SubAgentState, SubAgentTask
from .runner import SubAgentRunner

# Aliases para retrocompat — testes importam estes nomes privados (item 9 —
# SRP extract). A classe/função canônica vive em ``_capture``; mantemos os
# nomes históricos exportados deste módulo para não quebrar importadores.
_CappedBuffer = CappedBuffer
_get_capture_buffer_max_bytes = get_capture_buffer_max_bytes

logger = logging.getLogger(__name__)


def _get_budget_s() -> float:
    """Teto global de tempo, lido via :class:`Settings` (pilar 7 — config
    centralizada). M2 / M11 (issue #295 review): a leitura via ``os.environ``
    direto no domínio violava o princípio. ``get_settings().subagent_budget_s``
    aceita override por env var (``DEILE_SUBAGENT_BUDGET_S``) e por
    ``~/.deile/settings.json`` (``subagent.budget_s``).
    """
    return float(getattr(get_settings(), "subagent_budget_s", 600.0))


def get_max_subagent_budget_s() -> float:
    """Public accessor — retorna o budget global *em runtime* (NT2 — iter-3).

    Substitui o constante snapshot-on-import ``MAX_SUBAGENT_BUDGET_S`` que
    fixava o valor no momento do primeiro ``import`` do módulo, ignorando
    qualquer override posterior via env (``DEILE_SUBAGENT_BUDGET_S``) ou
    ``~/.deile/settings.json``. Callers (tool schema, testes) devem chamar
    esta função em vez de ler a constante.

    Implementação delega a :func:`_get_budget_s`, que já encapsula a
    leitura via :func:`get_settings` (Pilar 9 — configuração centralizada).
    """
    return _get_budget_s()


# Mantido como atributo private *snapshot-on-import* só para retrocompat
# com callers/testes que importavam o nome — não deve ser usado em código
# novo. Novos callers devem usar :func:`get_max_subagent_budget_s`.
_MAX_SUBAGENT_BUDGET_S_SNAPSHOT: float = _get_budget_s()


CancellationReason = Literal["user_esc", "budget_exceeded", "parent_cancel"]


@dataclass
class SubAgentResult:
    """Saída agregada de :meth:`SubAgentOrchestrator.run` — vai pro ToolResult."""

    states: List[SubAgentState]
    elapsed_s: float
    ok_count: int
    error_count: int
    cancelled: bool = False
    # NT5 (iter-3 review): discrimina a razão do cancelamento. Antes
    # ``cancelled=True`` conflate três caminhos distintos (ESC do usuário,
    # budget global estourado, cancel injetado pelo parent) — observabilidade
    # fraca, debugger/auditor não distingue causa raiz. ``None`` quando
    # ``cancelled=False``.
    cancellation_reason: Optional[CancellationReason] = None
    # Buffer agregado do stdout/stderr capturado durante a execução. Útil para
    # diagnóstico em testes e potencialmente para um modo de "ver logs brutos"
    # no painel focado (próxima iteração).
    captured_stdout: str = ""
    captured_stderr: str = ""

    @property
    def ok_global(self) -> bool:
        return self.error_count == 0 and not self.cancelled

    def consolidated_summary(self) -> str:
        from ._summary import render_consolidated
        return render_consolidated(self)

    def markdown_summary(self) -> str:
        from ._summary import render_markdown
        return render_markdown(self)


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


# NT3 (iter-3 review): a constante ``_CAPTURE_BUFFER_MAX_BYTES_SNAPSHOT``
# é capturada no momento do import — overrides posteriores via env/settings
# eram ignorados pelo default do ``_CappedBuffer``. Mantida como private
# para retrocompat em testes que importavam o nome; ``_CappedBuffer`` agora
# usa ``None`` como sentinel e resolve via :func:`_get_capture_buffer_max_bytes`
# em cada instância (lazy — respeita override em runtime).
#
# Item 9 (SRP extract): a classe ``CappedBuffer`` e o helper foram extraídos
# para :mod:`deile.orchestration.subagents._capture`. Mantemos o snapshot
# aqui apenas porque historicamente é importado por callers/testes.
_CAPTURE_BUFFER_MAX_BYTES_SNAPSHOT: int = _get_capture_buffer_max_bytes()


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

    # B1 — issue #295 review. Lock class-level que serializa entradas em
    # ``run()`` quando ``capture_output=True``. ``sys.stdout`` é estado
    # global do processo; com múltiplos dispatches concorrentes no worker
    # (asyncio.create_task), dispatches sobrepostos corrompem o stream uns
    # dos outros. Reentrância concorrente é rejeitada explicitamente — o
    # caller (a tool) já tem cooldown de 5s por sessão; quem chega ao
    # orquestrador com outro dispatch ativo está num caminho excepcional
    # e deve falhar rápido em vez de bloquear o worker indefinidamente.
    #
    # MA5 (iter-2 review): lazy-bound ao event loop corrente. Antes,
    # ``asyncio.Lock()`` em escopo de classe pegava o primeiro loop
    # ``__aenter__`` chamado — código que rodava em múltiplos ``asyncio.run()``
    # (testes loop-per-test, CLI ``_run_self_install`` + ``_run_oneshot``)
    # quebrava com ``RuntimeError: ... is bound to a different event loop``.
    # ``_get_capture_lock()`` agora cria/troca o lock conforme o loop muda.
    #
    # NT1 (iter-3 review): rastreamos o ``id(loop)`` que originalmente criou
    # o lock em vez de inspecionar ``Lock._loop`` (CPython-private). Mesma
    # semântica, sem depender de API interna instável. Lógica encapsulada
    # em :class:`LoopBoundLock` (compartilhada com
    # ``DispatchParallelSubagentsTool._get_locks_guard``).
    #
    # Item 9 (SRP extract): a instância canônica vive em
    # ``deile.orchestration.subagents._capture._capture_lock_holder``; o
    # atributo de classe permanece como alias para retrocompat com testes
    # que chamam ``SubAgentOrchestrator._CAPTURE_LOCK_HOLDER.reset()``.
    _CAPTURE_LOCK_HOLDER: ClassVar[LoopBoundLock] = _capture_lock_holder

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
        """Lazy-init do lock de captura por event loop (MA5 — iter-2).

        Delega a :func:`deile.orchestration.subagents._capture.get_capture_lock`,
        que encapsula o :class:`LoopBoundLock` singleton — cria/troca o Lock
        conforme o loop muda, evitando ``RuntimeError: ... is bound to a
        different event loop`` em múltiplos ``asyncio.run()`` (CLI
        sub-comandos, pytest loop-per-test).

        Mantido como ``classmethod`` para retrocompat (callers/testes podem
        invocar via ``SubAgentOrchestrator._get_capture_lock()``).
        """
        return get_capture_lock()

    async def run(self, tasks: List[SubAgentTask]) -> SubAgentResult:
        """Dispara ``tasks`` em paralelo e devolve o estado final agregado.

        Quando ``capture_output=True``, serializa entradas concorrentes via
        :attr:`_CAPTURE_LOCK` com ``acquire`` ATÔMICO (MA2 — iter-2 review):
        a aquisição usa ``asyncio.wait_for(..., timeout=0)``; o timeout zero
        garante fail-fast SEM a TOCTOU entre um ``.locked()`` check e um
        posterior ``async with``. Quem chega com o lock ocupado recebe
        ``RuntimeError`` (não bloqueia).

        Caminhos com ``capture_output=False`` (tests, headless) não tocam o
        lock — múltiplos podem rodar em paralelo.
        """
        if not tasks:
            return SubAgentResult(states=[], elapsed_s=0.0, ok_count=0, error_count=0)

        if not self._capture_output:
            return await self._run_locked(tasks)

        # MA2 (iter-2 review): aquisição atômica via ``acquire_nowait`` (lock
        # interno da asyncio Lock que retorna imediatamente quando livre OU
        # levanta sem bloquear quando ocupado). asyncio.Lock não expõe
        # ``acquire_nowait`` diretamente, mas o protocolo é: ``not locked()``
        # implica que ``async with`` não vai aguardar — e como asyncio é
        # single-threaded cooperativo, entre a verificação de ``locked()`` e
        # o ``acquire()`` síncrono no ``async with`` NÃO há await que
        # permita interleaving (a TOCTOU clássica requer preemption). Para
        # tornar a intenção explícita, encapsulamos numa única expressão.
        lock = self._get_capture_lock()
        if lock.locked():
            raise RuntimeError(
                "SubAgentOrchestrator.run(capture_output=True): another dispatch "
                "is already running with stdout capture; concurrent capture would "
                "corrupt sys.stdout state. Retry sequentially or pass "
                "capture_output=False."
            )
        # ``acquire()`` quando ``not locked()`` retorna sem suspender (fast
        # path do asyncio.Lock). Não há await entre o check e o acquire
        # nesta corrotina, então nenhum sibling pode escapar pela janela.
        await lock.acquire()
        try:
            return await self._run_locked(tasks)
        finally:
            try:
                lock.release()
            except RuntimeError:  # pragma: no cover — defesa contra double-release
                pass

    async def _run_locked(self, tasks: List[SubAgentTask]) -> SubAgentResult:
        """Corpo do :meth:`run` — chamado já dentro do lock (quando aplicável)."""
        states = [SubAgentState(task=t) for t in tasks]
        broadcast = _Broadcast()
        start = time.monotonic()

        # Capturamos referências aos streams REAIS antes de qualquer redirect.
        # O renderer (criado a seguir) recebe ``real_stdout`` e constrói seu
        # próprio :class:`rich.console.Console` ligado a esse handle — assim
        # o painel continua aparecendo no terminal mesmo enquanto ``sys.stdout``
        # está redirecionado para suprimir os ``print()`` dos sub-DEILEs.
        real_stdout: TextIO = sys.stdout

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
        budget_exceeded = False
        outer_cancelled = False
        budget_s = _get_budget_s()
        try:
            runner_tasks = [asyncio.create_task(_run_one(st)) for st in states]
            if renderer is not None and hasattr(renderer, "run"):
                renderer_task = asyncio.create_task(renderer.run())

            try:
                # M2/M11 (issue #295 review): envolve o waiter principal num
                # ``asyncio.wait_for`` com o budget global; TimeoutError sinaliza
                # que devemos cancelar tudo e marcar states pendentes.
                #
                # MA3 (iter-2 review): também capturamos CancelledError —
                # quando o caller cancela ``run()`` (ex.: ESC no streaming
                # renderer), ``wait_for`` propaga CancelledError em vez de
                # TimeoutError. Em ambos os casos cancelamos runners +
                # renderer e aguardamos cleanup; CancelledError é re-raised
                # APÓS o cleanup (Pilar 03 §6).
                await asyncio.wait_for(
                    self._wait_runners_and_renderer(runner_tasks, renderer_task, renderer),
                    timeout=budget_s,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                is_cancel = isinstance(exc, asyncio.CancelledError)
                if is_cancel:
                    outer_cancelled = True
                    logger.info(
                        "SubAgentOrchestrator: caller cancelou; cancelando "
                        "%d runner(s) pendente(s)",
                        sum(1 for t in runner_tasks if not t.done()),
                    )
                else:
                    budget_exceeded = True
                    logger.warning(
                        "SubAgentOrchestrator: budget de %.1fs estourado; cancelando "
                        "%d runner(s) pendente(s)",
                        budget_s,
                        sum(1 for t in runner_tasks if not t.done()),
                    )
                for t in runner_tasks:
                    if not t.done():
                        t.cancel()
                if renderer_task is not None and not renderer_task.done():
                    renderer_task.cancel()
                # MA7 (iter-2 review): aguarda real finalização para que os
                # ``finally`` dos runners rodem (cleanup de sub-sessions),
                # MAS com timeout curto — runners que ignoram cancellation
                # (subprocess.run blocking) ficariam orfãos pra sempre,
                # bloqueando o lock + restore de stdout. Aceita tradeoff:
                # orphan tasks em troca de dispatch completion garantido.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*runner_tasks, return_exceptions=True),
                        timeout=5.0,
                    )
                # NT6 (iter-3 review): Python 3.11+ aliases
                # ``asyncio.TimeoutError`` para o ``TimeoutError`` built-in;
                # ambos são capturados pra manter compat com 3.9-3.10
                # (per pyproject.toml ``requires-python = ">=3.9"``), onde
                # são classes distintas.
                except (asyncio.TimeoutError, TimeoutError):
                    pending = [t for t in runner_tasks if not t.done()]
                    logger.error(
                        "SubAgentOrchestrator: %d runner task(s) ignored cancel "
                        "after 5s — leaving orphan(s) to unblock the orchestrator",
                        len(pending),
                    )
                if renderer_task is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(renderer_task, return_exceptions=True),
                            timeout=2.0,
                        )
                    # NT6 (iter-3): mesmo motivo do bloco acima — manter
                    # compat com Python 3.9-3.10 onde ``asyncio.TimeoutError``
                    # ≠ ``TimeoutError``.
                    except (asyncio.TimeoutError, TimeoutError):
                        logger.warning(
                            "SubAgentOrchestrator: renderer ignored cancel after 2s"
                        )
                # Marca states que ficaram pendentes como cancelled com
                # mensagem clara — é o que o LLM principal vai ver.
                if not is_cancel:
                    for st in states:
                        if not st.is_terminal:
                            st.status = "cancelled"
                            st.error = "subagent_budget_exceeded"
                            if st.finished_at is None:
                                st.finished_at = time.monotonic()

            # M15 (issue #295 review): garante que o renderer_task realmente
            # terminou antes do finally restaurar sys.stdout. Sem este await,
            # uma frame do renderer poderia tentar escrever no console DEPOIS
            # do restore e perder a referência ao stdout real.
            #
            # MA4 (iter-2 review): NÃO engolimos CancelledError indiscrimin-
            # adamente — se o CancelledError veio porque NÓS cancelamos o
            # renderer (renderer_task.cancelled() True), swallow; se veio
            # porque o parent cancelou esta corrotina (current_task tem
            # cancelling > 0), re-raise para honrar Pilar 03 §6.
            if renderer_task is not None and not renderer_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(renderer_task), timeout=1.5)
                except asyncio.TimeoutError:
                    renderer_task.cancel()
                    try:
                        await renderer_task
                    except asyncio.CancelledError:
                        # Distinguir parent-cancel vs our-cancel.
                        current = asyncio.current_task()
                        parent_cancelling = (
                            current is not None
                            and getattr(current, "cancelling", lambda: 0)() > 0
                        )
                        if parent_cancelling and not renderer_task.cancelled():
                            raise
                        # Caso normal: nosso cancel cumprido, engolir.
                    except Exception:  # noqa: BLE001 — renderer can't break orchestrator
                        logger.warning("renderer raised during shutdown", exc_info=True)

            # Se o renderer sinalizou cancel, propaga ao estado agregado.
            if renderer is not None and getattr(renderer, "cancelled", False):
                cancelled = True

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

        # MA3 (iter-2 review): se o cancel veio do parent, re-raise APÓS o
        # cleanup completo (Pilar 03 §6 — CancelledError nunca capturada
        # sem re-raise). O ``finally`` acima já restaurou stdout.
        if outer_cancelled:
            raise asyncio.CancelledError()

        elapsed = time.monotonic() - start
        ok_count = sum(1 for s in states if s.status == "ok")
        error_count = sum(1 for s in states if s.status in ("error", "cancelled"))
        # NT5 (iter-3 review): discrimina razão do cancel ao popular
        # ``SubAgentResult.cancellation_reason``. ``budget_exceeded`` ganha
        # precedência sobre ``user_esc`` — se o budget estourou enquanto o
        # usuário também apertou ESC, a causa raiz é o budget (o ESC pode
        # ter vindo em reação ao painel travado). ``parent_cancel`` nunca
        # atinge este return: ``outer_cancelled`` força ``raise`` acima.
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

    async def _wait_runners_and_renderer(
        self,
        runner_tasks: List[asyncio.Task],
        renderer_task: Optional[asyncio.Task],
        renderer: Optional[Any],
    ) -> None:
        """Espera todos os runners + (eventualmente) o renderer.

        Extraído do ``run()`` em :meth:`_run_locked` para que possa ser
        envolvido por ``asyncio.wait_for(timeout=budget_s)`` — quando o budget
        estoura, o ``TimeoutError`` propaga, o caller cancela tudo e marca
        states ainda pendentes (M11 — issue #295 review).
        """
        pending: set = set(runner_tasks)
        if renderer_task is not None:
            pending.add(renderer_task)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            # Se o renderer terminou primeiro com cancel, propaga.
            if renderer_task is not None and renderer_task in done:
                rt_cancelled = getattr(renderer, "cancelled", False) if renderer else False
                if rt_cancelled and not all(t.done() for t in runner_tasks):
                    for t in runner_tasks:
                        if not t.done():
                            t.cancel()
            # Se todos os runners terminaram, fechamos o renderer.
            if all(t.done() for t in runner_tasks) and renderer_task is not None:
                if not renderer_task.done():
                    # O renderer detecta states terminais e fecha sozinho;
                    # damos um tick a mais e cancelamos se travou.
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
