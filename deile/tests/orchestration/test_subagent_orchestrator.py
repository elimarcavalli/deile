"""Tests for ``deile.orchestration.subagents.orchestrator`` (issue #257).

Foca no contrato do orquestrador:
  * ``asyncio.gather(return_exceptions=True)`` isola falhas — uma frente em
    erro não impede as outras de continuarem.
  * ``max_parallel`` respeita o semáforo.
  * ``consolidated_summary`` é breve (< 2KB) e mostra status + arquivos por
    frente — ele vai pro LLM, então deve ser barato em tokens.
  * Renderer factory é opcional (caminho headless funciona).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from deile.orchestration.subagents import SubAgentOrchestrator, SubAgentTask
from deile.orchestration.subagents.events import (SubAgentEvent,
                                                  SubAgentEventKind,
                                                  SubAgentState)
from deile.orchestration.subagents.runner import OnEvent

pytestmark = pytest.mark.unit


class _StubRunner:
    """Runner inerte que apenas marca a state como ok após N segundos."""

    def __init__(self, delays: dict, fail_for: set = frozenset()):
        # delays = {index: seconds}
        self._delays = delays
        self._fail_for = fail_for
        self.observed_concurrency: list[int] = []
        self._active = 0
        self._lock = asyncio.Lock()

    async def run_one(self, state: SubAgentState, *, on_event: OnEvent) -> None:
        async with self._lock:
            self._active += 1
            self.observed_concurrency.append(self._active)
        try:
            state.status = "running"
            state.started_at = time.monotonic()
            on_event(SubAgentEvent(
                kind=SubAgentEventKind.STARTED,
                index=state.task.index,
                label=state.task.description,
            ))
            delay = self._delays.get(state.task.index, 0.05)
            await asyncio.sleep(delay)
            if state.task.index in self._fail_for:
                state.status = "error"
                state.error = "stub failure"
                state.finished_at = time.monotonic()
                on_event(SubAgentEvent(
                    kind=SubAgentEventKind.FAILED,
                    index=state.task.index,
                    label="boom",
                    error="stub failure",
                ))
                return
            state.status = "ok"
            state.result_text = f"done #{state.task.index}"
            state.add_file(f"file_{state.task.index}.py")
            state.finished_at = time.monotonic()
            on_event(SubAgentEvent(
                kind=SubAgentEventKind.COMPLETED,
                index=state.task.index,
                label="ok",
            ))
        finally:
            async with self._lock:
                self._active -= 1


def _mk_tasks(n: int) -> list[SubAgentTask]:
    return [
        SubAgentTask(index=i, description=f"task {i}", prompt=f"prompt for task #{i}" * 5)
        for i in range(1, n + 1)
    ]


async def test_runs_all_tasks_in_parallel_under_semaphore_cap():
    runner = _StubRunner(delays={1: 0.10, 2: 0.10, 3: 0.10})
    orch = SubAgentOrchestrator(runner, max_parallel=2)
    tasks = _mk_tasks(3)

    result = await orch.run(tasks)

    assert result.ok_count == 3
    assert result.error_count == 0
    assert result.ok_global is True
    # max_parallel=2 implica que a fila viu no máximo 2 simultâneas em algum
    # ponto, e nunca 3.
    assert max(runner.observed_concurrency) <= 2
    assert max(runner.observed_concurrency) >= 1


async def test_failure_does_not_cancel_siblings():
    """gather(return_exceptions=True) garante isolamento entre frentes."""
    runner = _StubRunner(delays={1: 0.02, 2: 0.02, 3: 0.02}, fail_for={2})
    orch = SubAgentOrchestrator(runner, max_parallel=3)
    tasks = _mk_tasks(3)

    result = await orch.run(tasks)

    # 2 ok + 1 erro; ok_global é False mas as outras completaram.
    assert result.ok_count == 2
    assert result.error_count == 1
    assert result.ok_global is False
    statuses = {s.task.index: s.status for s in result.states}
    assert statuses == {1: "ok", 2: "error", 3: "ok"}


async def test_consolidated_summary_is_compact_and_informative():
    runner = _StubRunner(delays={1: 0.01, 2: 0.01}, fail_for={2})
    orch = SubAgentOrchestrator(runner, max_parallel=2)
    tasks = _mk_tasks(2)

    result = await orch.run(tasks)
    summary = result.consolidated_summary()

    # Cabeçalho com contadores + uma linha por frente.
    assert "2 frentes" not in summary  # não inventamos plural; é livre
    assert "1 ok" in summary
    assert "1 erro" in summary
    assert "task 1" in summary
    assert "task 2" in summary
    # Curto pra não saturar o contexto do LLM.
    assert len(summary) < 2000


async def test_renderer_factory_is_optional_and_invoked_when_provided():
    runner = _StubRunner(delays={1: 0.01, 2: 0.01})
    captured = {}

    class _FakeRenderer:
        # Issue #257 round 2: factory agora aceita (states, broadcast, real_stdout)
        def __init__(self, states, broadcast, real_stdout=None):
            captured["states"] = states
            captured["broadcast"] = broadcast
            captured["real_stdout"] = real_stdout

        async def run(self):
            # Encerra rápido para não segurar a finalização.
            await asyncio.sleep(0.005)

    orch = SubAgentOrchestrator(
        runner, max_parallel=2, renderer_factory=_FakeRenderer
    )
    tasks = _mk_tasks(2)

    result = await orch.run(tasks)

    assert result.ok_count == 2
    assert "states" in captured and len(captured["states"]) == 2
    assert captured["broadcast"] is not None
    # real_stdout deve ser repassado (sys.stdout durante o teste)
    assert captured["real_stdout"] is not None


async def test_renderer_factory_backward_compat_with_2_args():
    """Factory antiga (states, broadcast) ainda funciona — TypeError → retry."""
    runner = _StubRunner(delays={1: 0.005})

    class _OldRenderer:
        def __init__(self, states, broadcast):  # sem real_stdout
            self.states = states

        async def run(self):
            await asyncio.sleep(0.001)

    orch = SubAgentOrchestrator(
        runner, max_parallel=1, renderer_factory=_OldRenderer
    )
    result = await orch.run(_mk_tasks(2))
    assert result.ok_count == 2


async def test_capture_output_suppresses_print_from_subagent():
    """Quando capture_output=True (default), print() durante o runner vai
    para o buffer interno em vez de poluir o terminal do usuário.
    """
    import sys as _sys

    class _PrintingRunner:
        async def run_one(self, state, *, on_event):
            state.status = "running"
            state.started_at = 0.0
            print("OUTPUT FROM SUBAGENT", flush=True)
            print("STDERR FROM SUBAGENT", file=_sys.stderr, flush=True)
            state.status = "ok"
            state.finished_at = 0.05

    orch = SubAgentOrchestrator(_PrintingRunner(), max_parallel=1, capture_output=True)
    result = await orch.run(_mk_tasks(2))

    assert "OUTPUT FROM SUBAGENT" in result.captured_stdout
    assert "STDERR FROM SUBAGENT" in result.captured_stderr
    # Após o run, sys.stdout/stderr foram restaurados (smoke test).
    assert _sys.stdout is not None
    assert _sys.stderr is not None


async def test_capture_output_false_does_not_redirect():
    """capture_output=False mantém prints fluindo para o terminal real
    (usado em testes onde queremos VER o output do runner).
    """
    class _NoopRunner:
        async def run_one(self, state, *, on_event):
            state.status = "ok"
            state.finished_at = 0.001

    orch = SubAgentOrchestrator(_NoopRunner(), max_parallel=1, capture_output=False)
    result = await orch.run(_mk_tasks(2))
    assert result.captured_stdout == ""
    assert result.captured_stderr == ""


async def test_capped_buffer_truncates_oversize_writes():
    """Fix C5: ``_CappedBuffer`` substitui StringIO unbounded — após o cap,
    descarta o resto e injeta marker ``[...truncated]``."""
    from deile.orchestration.subagents.orchestrator import _CappedBuffer
    buf = _CappedBuffer(max_bytes=100)
    for _ in range(50):
        buf.write("x" * 10)  # 500 chars total
    content = buf.getvalue()
    assert len(content) < 200  # bem abaixo de 500
    assert "[...truncated]" in content
    # Continua aceitando writes (report success) sem estourar
    buf.write("more")
    # write() retorna len(s) por contrato fileio mesmo após cap
    assert buf.write("test") == 4


async def test_capped_buffer_below_cap_keeps_everything():
    from deile.orchestration.subagents.orchestrator import _CappedBuffer
    buf = _CappedBuffer(max_bytes=1024)
    buf.write("hello ")
    buf.write("world")
    assert buf.getvalue() == "hello world"
    assert "[...truncated]" not in buf.getvalue()


async def test_markdown_summary_format():
    """``markdown_summary`` produz markdown adequado para renderização no /resume."""
    runner = _StubRunner(delays={1: 0.001, 2: 0.001}, fail_for={2})
    orch = SubAgentOrchestrator(runner, max_parallel=2)
    result = await orch.run(_mk_tasks(2))

    md = result.markdown_summary()
    assert "**Sub-DEILEs paralelos**" in md
    assert "✅" in md and "❌" in md
    assert "#1" in md and "#2" in md
    assert "**task 1**" in md or "task 1" in md
    # Arquivos do task ok aparecem como inline-code
    assert "`file_1.py`" in md


async def test_empty_tasks_returns_empty_result():
    runner = _StubRunner(delays={})
    orch = SubAgentOrchestrator(runner)
    result = await orch.run([])
    assert result.ok_count == 0
    assert result.error_count == 0
    assert result.elapsed_s == 0.0
    assert result.states == []


# ── Regression tests para PR #295 review ──────────────────────────────────


async def test_concurrent_capture_dispatches_rejected(monkeypatch):
    """B1 (PR #295 review): com ``capture_output=True``, dispatches concorrentes
    devem ser rejeitados (RuntimeError) — sys.stdout é global do processo e
    sobreposição corromperia o stream.
    """
    # Runner que segura por um tempo para garantir overlap.
    class _HoldRunner:
        async def run_one(self, state, *, on_event):
            state.status = "running"
            state.started_at = time.monotonic()
            await asyncio.sleep(0.2)
            state.status = "ok"
            state.finished_at = time.monotonic()

    orch1 = SubAgentOrchestrator(_HoldRunner(), max_parallel=1, capture_output=True)
    orch2 = SubAgentOrchestrator(_HoldRunner(), max_parallel=1, capture_output=True)

    async def _dispatch_first():
        return await orch1.run(_mk_tasks(2))

    # Dispatcher 1 entra primeiro; espera mínima para o lock ser adquirido.
    t1 = asyncio.create_task(_dispatch_first())
    await asyncio.sleep(0.02)

    # Segundo dispatcher concorrente DEVE falhar imediatamente.
    with pytest.raises(RuntimeError, match="another dispatch is already running"):
        await orch2.run(_mk_tasks(2))

    # Primeiro segue normalmente.
    result = await t1
    assert result.ok_count == 2


async def test_no_lock_when_capture_disabled():
    """capture_output=False não deve acionar o lock — testes/headless rodam
    concorrentemente sem mutar sys.stdout.
    """
    class _NoopRunner:
        async def run_one(self, state, *, on_event):
            state.status = "ok"
            state.finished_at = time.monotonic()

    # Dois orquestradores capture=False rodando em paralelo devem ok.
    orch1 = SubAgentOrchestrator(_NoopRunner(), capture_output=False)
    orch2 = SubAgentOrchestrator(_NoopRunner(), capture_output=False)
    r1, r2 = await asyncio.gather(orch1.run(_mk_tasks(2)), orch2.run(_mk_tasks(2)))
    assert r1.ok_count == 2 and r2.ok_count == 2


async def test_budget_enforcement_cancels_pending_states(monkeypatch):
    """M2/M11 (PR #295 review): com budget pequeno, runners que travam são
    cancelados; states ainda pendentes são marcados como ``cancelled`` com
    erro ``subagent_budget_exceeded``.
    """
    # Substitui o getter de budget por uma janela curta para o teste.
    monkeypatch.setattr(
        "deile.orchestration.subagents.orchestrator._get_budget_s",
        lambda: 0.3,
    )

    class _HangRunner:
        """Runner que nunca termina (até ser cancelado)."""
        async def run_one(self, state, *, on_event):
            state.status = "running"
            state.started_at = time.monotonic()
            try:
                await asyncio.sleep(10.0)
                state.status = "ok"
                state.finished_at = time.monotonic()
            except asyncio.CancelledError:
                # Runner correto: marca cancelled e re-raise
                state.status = "cancelled"
                state.finished_at = time.monotonic()
                raise

    orch = SubAgentOrchestrator(_HangRunner(), max_parallel=2, capture_output=False)
    result = await orch.run(_mk_tasks(2))

    # Budget estourou → cancelled global
    assert result.cancelled is True
    # States ficaram cancelled. (Pode ser "cancelled" do runner ou marcado
    # pelo orquestrador como subagent_budget_exceeded se runner não capturou.)
    for st in result.states:
        assert st.status in ("cancelled",)


async def test_outer_cancel_cancels_runners_and_reraises_cancelled_error():
    """MA3 (iter-2): cancel injetado pelo caller propaga corretamente.

    Antes: ``asyncio.wait_for`` só capturava ``TimeoutError`` — um
    ``CancelledError`` injetado pelo caller pulava o bloco de cancel dos
    runners, deixando tasks órfãs vivas e violando Pilar 03 §6.
    """
    runner_cancelled = {"flag": False}

    class _LongRunner:
        async def run_one(self, state, *, on_event):
            state.status = "running"
            state.started_at = time.monotonic()
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                runner_cancelled["flag"] = True
                state.status = "cancelled"
                state.finished_at = time.monotonic()
                raise

    orch = SubAgentOrchestrator(_LongRunner(), max_parallel=2, capture_output=False)
    task = asyncio.create_task(orch.run(_mk_tasks(2)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Runner foi efetivamente cancelado pelo orchestrator antes do re-raise.
    assert runner_cancelled["flag"] is True


async def test_budget_exceeded_with_noncooperative_runner_marks_pending(monkeypatch):
    """MA7 + minor coverage: runner que NÃO captura CancelledError ainda
    permite o orchestrator marcar states como ``subagent_budget_exceeded``
    (path do orchestrator.py:392), e o gather final tem timeout bounded.
    """
    monkeypatch.setattr(
        "deile.orchestration.subagents.orchestrator._get_budget_s",
        lambda: 0.15,
    )

    class _NonCoopRunner:
        """Runner que NÃO propaga CancelledError corretamente — engole."""
        async def run_one(self, state, *, on_event):
            state.status = "running"
            state.started_at = time.monotonic()
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                # Engole — não re-raise (anti-padrão proposital pro teste).
                # State fica "running" — orchestrator deve marcá-lo cancelled.
                return

    orch = SubAgentOrchestrator(_NonCoopRunner(), max_parallel=2, capture_output=False)
    result = await orch.run(_mk_tasks(2))
    assert result.cancelled is True
    # Branch crítico: orchestrator marcou error='subagent_budget_exceeded'
    # para states que ficaram em running quando runner não cooperou.
    assert any(
        st.error == "subagent_budget_exceeded" for st in result.states
    ), f"states: {[(s.status, s.error) for s in result.states]}"


def test_lazy_capture_lock_per_event_loop():
    """MA5 (iter-2): _CAPTURE_LOCK é lazy-bound ao event loop corrente.

    Antes, asyncio.Lock() em escopo de classe pegava o loop do primeiro
    __aenter__. Múltiplos asyncio.run() (e.g. CLI subcommands, pytest
    loop-per-test) batiam em RuntimeError. O lazy-init re-cria por loop.

    Síncrono propositalmente para usar asyncio.run() — asyncio_mode=auto
    do pytest já provê um loop ativo que conflitaria com o nested run().
    """
    class _NoopRunner:
        async def run_one(self, state, *, on_event):
            state.status = "ok"
            state.finished_at = time.monotonic()

    async def _scenario():
        orch = SubAgentOrchestrator(_NoopRunner(), capture_output=True)
        return await orch.run(_mk_tasks(2))

    # Limpa qualquer lock-state prévio de outro teste.
    SubAgentOrchestrator._CAPTURE_LOCK = None

    # Rodar em dois loops sucessivos — antes seria RuntimeError no segundo.
    r1 = asyncio.run(_scenario())
    r2 = asyncio.run(_scenario())
    assert r1.ok_count == 2
    assert r2.ok_count == 2


async def test_capture_output_false_does_not_redirect_via_capsys(capsys):
    """Reforço da cobertura (iter-2 review): capture_output=False não
    redireciona sys.stdout — prints fluem para o stdout do processo
    (captado por capsys neste teste).
    """
    class _PrintRunner:
        async def run_one(self, state, *, on_event):
            print("VISIBLE_FROM_RUNNER")
            state.status = "ok"
            state.finished_at = time.monotonic()

    orch = SubAgentOrchestrator(_PrintRunner(), max_parallel=1, capture_output=False)
    result = await orch.run(_mk_tasks(2))
    assert result.ok_count == 2
    # capsys captura prints reais que escaparam pro stdout do processo.
    captured = capsys.readouterr()
    assert "VISIBLE_FROM_RUNNER" in captured.out
    # E result.captured_stdout permanece vazio porque não houve redirect.
    assert result.captured_stdout == ""


async def test_renderer_task_awaited_before_stdout_restore():
    """M15 (PR #295 review): após cancel/normal, o renderer_task deve ser
    aguardado antes do finally restaurar sys.stdout. Sem isto, uma frame
    final do renderer poderia escrever no ``Console(file=real_stdout)`` que
    já foi restaurado fora do contexto do orquestrador.
    """
    import sys as _sys
    saved_stdout = _sys.stdout

    class _SlowRenderer:
        def __init__(self, states, broadcast, real_stdout=None):
            self._states = states
            self.cancelled = False
            self._ran_full = False

        async def run(self):
            # Renderer toma um pouco a mais que os runners.
            try:
                await asyncio.sleep(0.15)
                self._ran_full = True
            except asyncio.CancelledError:
                raise

    class _FastRunner:
        async def run_one(self, state, *, on_event):
            state.status = "running"
            state.started_at = time.monotonic()
            await asyncio.sleep(0.02)
            state.status = "ok"
            state.finished_at = time.monotonic()

    captured = {}

    def _factory(states, broadcast, real_stdout=None):
        renderer = _SlowRenderer(states, broadcast, real_stdout)
        captured["renderer"] = renderer
        return renderer

    orch = SubAgentOrchestrator(
        _FastRunner(),
        max_parallel=2,
        renderer_factory=_factory,
        capture_output=True,
    )
    result = await orch.run(_mk_tasks(2))

    assert result.ok_count == 2
    # Stdout restaurado ao final
    assert _sys.stdout is saved_stdout
