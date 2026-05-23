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

from deile.orchestration.subagents import (SubAgentOrchestrator,
                                           SubAgentTask)
from deile.orchestration.subagents.events import (SubAgentEvent,
                                                  SubAgentEventKind,
                                                  SubAgentState)
from deile.orchestration.subagents.runner import OnEvent, SubAgentRunner


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
        def __init__(self, states, broadcast):
            captured["states"] = states
            captured["broadcast"] = broadcast

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


async def test_empty_tasks_returns_empty_result():
    runner = _StubRunner(delays={})
    orch = SubAgentOrchestrator(runner)
    result = await orch.run([])
    assert result.ok_count == 0
    assert result.error_count == 0
    assert result.elapsed_s == 0.0
    assert result.states == []
