"""Tests para ``WorkerSubAgentRunner`` (issue #257).

Foco: ciclo de polling, terminal-detection, tolerância a 404 transiente,
agregação de ``files`` no final, e captura de exceção.
"""
from __future__ import annotations

from collections import deque

import pytest

from deile.infrastructure.deile_worker_client import WorkerDispatchError
from deile.orchestration.subagents.events import (SubAgentEventKind,
                                                  SubAgentState, SubAgentTask)
from deile.orchestration.subagents.runner import WorkerSubAgentRunner


pytestmark = pytest.mark.unit


class _StubWorkerClient:
    """Cliente que devolve respostas pré-roteirizadas."""

    def __init__(
        self,
        dispatch_return,
        progress_snapshots: list,
        result_return=None,
        progress_raises: dict | None = None,
    ):
        self._dispatch = dispatch_return
        self._progress = deque(progress_snapshots)
        self._result = result_return
        self._progress_raises = progress_raises or {}
        self.poll_count = 0

    async def dispatch(self, payload, *, wait):
        return self._dispatch

    async def get_progress(self, task_id):
        self.poll_count += 1
        if self.poll_count in self._progress_raises:
            raise self._progress_raises[self.poll_count]
        if not self._progress:
            return {"ok": False, "error": "no more snapshots"}
        return self._progress.popleft()

    async def get_result(self, task_id):
        return self._result or {}


def _task(index=1) -> SubAgentTask:
    return SubAgentTask(
        index=index,
        description="task",
        prompt="prompt suficientemente longo para passar do mínimo defensivo",
    )


async def test_polls_until_terminal_and_aggregates_files():
    client = _StubWorkerClient(
        dispatch_return={"task_id": "abc123", "status": "running"},
        progress_snapshots=[
            {"ok": None, "progress_lines": ["tool_invoked:read_file"], "current_activity": "reading"},
            {"ok": None, "progress_lines": ["tool_invoked:read_file", "tool_completed:read_file"],
             "current_activity": "done reading"},
            {"ok": True, "progress_lines": ["tool_invoked:read_file", "tool_completed:read_file"],
             "files": ["foo.py", "bar.py"]},
        ],
        result_return={"files": ["foo.py", "bar.py"], "summary": "all good"},
    )
    runner = WorkerSubAgentRunner(client, session_id="s1", poll_interval_s=0.01)
    state = SubAgentState(task=_task(index=1))

    captured: list = []
    await runner.run_one(state, on_event=captured.append)

    assert state.status == "ok"
    assert state.task_id == "abc123"
    assert "foo.py" in state.files_touched
    assert "bar.py" in state.files_touched
    assert state.result_text == "all good"
    # Pelo menos: STARTED + PROGRESS(task_id) + 2 PROGRESS lines + COMPLETED
    kinds = [e.kind for e in captured]
    assert SubAgentEventKind.STARTED in kinds
    assert SubAgentEventKind.COMPLETED in kinds


async def test_terminal_failure_marks_error_with_message():
    client = _StubWorkerClient(
        dispatch_return={"task_id": "abc"},
        progress_snapshots=[
            {"ok": False, "error": "worker timeout"},
        ],
    )
    runner = WorkerSubAgentRunner(client, session_id="s", poll_interval_s=0.01)
    state = SubAgentState(task=_task(index=2))

    await runner.run_one(state, on_event=lambda _: None)

    assert state.status == "error"
    assert state.error and "worker timeout" in state.error


async def test_404_transient_does_not_abort_polling():
    """Logo após o dispatch, o worker pode responder 404 antes de criar o state."""
    client = _StubWorkerClient(
        dispatch_return={"task_id": "abc"},
        progress_snapshots=[
            # Após o 404 — pula direto para terminal
            {"ok": True, "progress_lines": []},
        ],
        progress_raises={
            1: WorkerDispatchError("task not found", error_code="NOT_FOUND"),
        },
    )
    runner = WorkerSubAgentRunner(client, session_id="s", poll_interval_s=0.01)
    state = SubAgentState(task=_task(index=3))

    await runner.run_one(state, on_event=lambda _: None)

    assert state.status == "ok"
    assert client.poll_count >= 2


async def test_dispatch_failure_marks_state_error_without_propagating():
    """Erro no dispatch inicial vira state.error; orquestrador não vê exceção."""

    class _FailingClient:
        async def dispatch(self, payload, *, wait):
            raise WorkerDispatchError("auth missing", error_code="WORKER_AUTH_MISSING")

        async def get_progress(self, task_id):
            return {}

        async def get_result(self, task_id):
            return {}

    runner = WorkerSubAgentRunner(_FailingClient(), session_id="s", poll_interval_s=0.01)
    state = SubAgentState(task=_task(index=4))

    captured: list = []
    await runner.run_one(state, on_event=captured.append)

    assert state.status == "error"
    assert "auth missing" in (state.error or "")
    assert any(e.kind is SubAgentEventKind.FAILED for e in captured)


async def test_no_task_id_in_dispatch_response_treated_as_error():
    client = _StubWorkerClient(
        dispatch_return={},  # sem task_id
        progress_snapshots=[],
    )
    runner = WorkerSubAgentRunner(client, session_id="s", poll_interval_s=0.01)
    state = SubAgentState(task=_task(index=5))

    await runner.run_one(state, on_event=lambda _: None)

    assert state.status == "error"
    assert state.error and "task_id" in state.error
