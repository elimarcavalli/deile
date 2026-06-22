"""Tests for WorkflowExecutor — covers issues #140, #141, #142."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.exceptions import DEILEError
from deile.orchestration.sqlite_task_manager import Task, TaskList, TaskStatus
from deile.orchestration.workflow_executor import (WorkflowExecutor,
                                                   WorkflowStep)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id="t001", title="Test Task", status=TaskStatus.TODO, metadata=None):
    t = Task(id=task_id, title=title, status=status, metadata=metadata or {})
    return t


def _make_task_list(list_id="list1", title="Test Workflow", total=1):
    tl = TaskList(id=list_id, title=title)
    tl.total_tasks = total
    tl.stop_on_failure = True
    return tl


def _make_task_manager(
    task_list=None,
    tasks_sequence=None,  # list of list-of-tasks returned on successive get_next_tasks calls
):
    """Returns a mock SQLiteTaskManager."""
    mgr = MagicMock()
    tl = task_list or _make_task_list()
    mgr.create_task_list = AsyncMock(return_value=tl)
    mgr.add_task_to_list = AsyncMock(side_effect=lambda **kw: _make_task(
        task_id="t001", title=kw.get("title", "step"),
        metadata=kw.get("metadata", {}),
    ))
    mgr.activate_task_list = AsyncMock()
    mgr.mark_task_completed = AsyncMock(return_value=True)
    mgr.load_task_list = AsyncMock(return_value=tl)
    mgr._get_tasks_for_list = AsyncMock(return_value=[])

    # get_next_tasks: empty by default (loop exits immediately)
    if tasks_sequence is not None:
        mgr.get_next_tasks = AsyncMock(side_effect=tasks_sequence)
    else:
        mgr.get_next_tasks = AsyncMock(return_value=[])

    mgr.get_task_list_status = AsyncMock(return_value={
        "id": "list1",
        "title": "Test Workflow",
        "active": True,
        "progress": 100.0,
        "total_tasks": 1,
        "completed_tasks": 1,
        "failed_tasks": 0,
        "current_task": None,
        "is_completed": True,
        "has_failures": False,
        "next_tasks": [],
    })
    return mgr


def _make_executor(task_manager=None, registry_has_tool=False):
    """Returns WorkflowExecutor with mocked dependencies."""
    mock_registry = MagicMock()
    mock_registry.get_enabled = MagicMock(
        return_value=MagicMock() if registry_has_tool else None
    )
    mock_registry.execute_tool = AsyncMock()

    with patch("deile.orchestration.workflow_executor.get_tool_registry", return_value=mock_registry):
        executor = WorkflowExecutor(task_manager=task_manager or _make_task_manager())

    executor._mock_registry = mock_registry
    return executor


# ---------------------------------------------------------------------------
# Issue #140 — start_workflow_execution must not call task_manager.start_execution
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.orchestration
async def test_start_workflow_execution_no_attribute_error():
    """start_workflow_execution must not raise AttributeError (#140)."""
    executor = _make_executor()
    result = await executor.start_workflow_execution("analyze and list files")

    assert "workflow_id" in result
    assert result["status"] == "started"


@pytest.mark.unit
@pytest.mark.orchestration
async def test_start_workflow_execution_calls_activate():
    """activate_task_list must be called on the task manager (#140)."""
    mgr = _make_task_manager()
    executor = _make_executor(task_manager=mgr)

    await executor.start_workflow_execution("list files")

    mgr.activate_task_list.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.orchestration
async def test_execute_task_list_loop_runs_tasks():
    """_execute_task_list_loop must invoke execute_task for each ready task."""
    task = _make_task(metadata={
        'list_id': 'list1',
        'action_type': 'tool',
        'action_name': 'read_file',
        'params': {},
    })
    # First call returns one task, second call returns [] to stop the loop
    mgr = _make_task_manager(tasks_sequence=[[task], []])
    executor = _make_executor(task_manager=mgr, registry_has_tool=True)

    # Patch execute_task to avoid real tool invocation
    executed = []

    async def fake_execute(t):
        executed.append(t.id)
        return {'success': True, 'data': {}, 'message': 'ok'}

    executor.execute_task = fake_execute

    await executor._execute_task_list_loop("list1")

    assert executed == ["t001"]
    call_kwargs = mgr.mark_task_completed.call_args.kwargs
    assert call_kwargs["list_id"] == "list1"
    assert call_kwargs["task_id"] == "t001"
    assert call_kwargs["success"] is True
    assert call_kwargs["error_message"] is None


@pytest.mark.unit
@pytest.mark.orchestration
async def test_execute_task_list_loop_stops_on_failure():
    """Loop must stop after a failed task when stop_on_failure=True (#140)."""
    task = _make_task(metadata={
        'list_id': 'list1',
        'action_type': 'tool',
        'action_name': 'read_file',
        'params': {},
    })
    tl = _make_task_list()
    tl.stop_on_failure = True
    mgr = _make_task_manager(task_list=tl, tasks_sequence=[[task]])
    executor = _make_executor(task_manager=mgr)

    async def failing_execute(t):
        return {'success': False, 'error': 'boom', 'message': 'fail'}

    executor.execute_task = failing_execute

    await executor._execute_task_list_loop("list1")

    mgr.mark_task_completed.assert_awaited_once_with(
        list_id="list1", task_id="t001", success=False,
        result_data=None, error_message="boom",
    )
    # get_next_tasks called once (before the failing task); loop exits without a second call
    assert mgr.get_next_tasks.await_count == 1


@pytest.mark.unit
@pytest.mark.orchestration
async def test_task_metadata_persisted_in_add_task():
    """metadata dict with list_id must be passed to add_task_to_list upfront (#140)."""
    mgr = _make_task_manager()
    executor = _make_executor(task_manager=mgr)

    step = WorkflowStep(action='list_files', params={'path': '.'}, description='List files', timeout=60)
    await executor._add_workflow_step_to_list(step, 'list1', 0)

    call_kwargs = mgr.add_task_to_list.call_args.kwargs
    assert 'metadata' in call_kwargs
    assert call_kwargs['metadata']['list_id'] == 'list1'
    assert call_kwargs['metadata']['action_name'] == 'list_files'


# ---------------------------------------------------------------------------
# Issue #141 — _execute_validation_action must implement real validation
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.orchestration
async def test_validation_action_passes_when_no_failures():
    """General validation passes when all previous steps succeeded (#141)."""
    task = _make_task(metadata={'list_id': 'list1'})
    completed = _make_task(task_id="prev", title="Previous Step", status=TaskStatus.COMPLETED)

    mgr = _make_task_manager()
    mgr._get_tasks_for_list = AsyncMock(return_value=[completed])
    executor = _make_executor(task_manager=mgr)

    result = await executor._execute_validation_action('general', {}, task)

    assert result['validation_passed'] is True


@pytest.mark.unit
@pytest.mark.orchestration
async def test_validation_action_fails_when_previous_step_failed():
    """General validation raises DEILEError when a previous step failed (#141)."""
    task = _make_task(task_id="validation_task", metadata={'list_id': 'list1'})
    failed_step = _make_task(task_id="prev", title="Broken Step", status=TaskStatus.FAILED)

    mgr = _make_task_manager()
    mgr._get_tasks_for_list = AsyncMock(return_value=[failed_step])
    executor = _make_executor(task_manager=mgr)

    with pytest.raises(DEILEError, match="General validation failed"):
        await executor._execute_validation_action('general', {}, task)


@pytest.mark.unit
@pytest.mark.orchestration
async def test_validation_action_unknown_type_raises():
    """Unknown validation_type must raise DEILEError (#141)."""
    task = _make_task(metadata={'list_id': 'list1'})
    executor = _make_executor()

    with pytest.raises(DEILEError, match="Unknown validation type"):
        await executor._execute_validation_action('nonexistent', {}, task)


@pytest.mark.unit
@pytest.mark.orchestration
async def test_validation_action_missing_list_id_raises():
    """General validation without list_id in metadata must raise DEILEError (#141)."""
    task = _make_task(metadata={})  # no list_id
    executor = _make_executor()

    with pytest.raises(DEILEError, match="list_id missing"):
        await executor._execute_validation_action('general', {}, task)


# ---------------------------------------------------------------------------
# Issue #142 — _execute_custom_action must raise instead of fake success
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.orchestration
async def test_unregistered_tool_action_raises():
    """Tool not in registry must raise DEILEError, not return fake success (#142)."""
    task = _make_task(metadata={
        'list_id': 'list1',
        'action_type': 'tool',
        'action_name': 'nonexistent_tool',
        'params': {},
    })
    executor = _make_executor(registry_has_tool=False)

    result = await executor.execute_task(task)

    # execute_task catches and returns error dict
    assert result['success'] is False
    assert "not found in registry" in result['error']


@pytest.mark.unit
@pytest.mark.orchestration
async def test_unknown_action_type_raises():
    """Unknown action_type in metadata must surface as task failure (#142)."""
    task = _make_task(metadata={
        'list_id': 'list1',
        'action_type': 'custom',   # removed handler
        'action_name': 'something',
        'params': {},
    })
    executor = _make_executor()

    result = await executor.execute_task(task)

    assert result['success'] is False
    assert "Unknown action type" in result['error']


# ---------------------------------------------------------------------------
# Fix #4 — wait_for_workflow_completion sai rapidamente em infra error (bug #779)
# Fix #10 — start_workflow_execution retorna total_steps correto (bug #779)
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.orchestration
async def test_wait_exits_promptly_on_infrastructure_error():
    """AC-4a/4b: Exception no loop → wait retorna em < 5s com has_failures=True."""
    import asyncio
    from datetime import timedelta

    infra_error = RuntimeError("db disk full")

    task = _make_task(task_id="t1", status=TaskStatus.TODO)
    task_list = _make_task_list(list_id="wf1", total=1)

    # Status cycle: primeiro TODO (loop pega a task), depois FAILED (loop termina)
    call_count = [0]

    async def _get_status(wf_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"is_completed": False, "has_failures": False}
        return {"is_completed": False, "has_failures": True}

    mgr = MagicMock()
    mgr.create_task_list = AsyncMock(return_value=task_list)
    mgr.add_task_to_list = AsyncMock(side_effect=lambda **kw: _make_task(
        task_id="t1", title=kw.get("title", "step"), metadata=kw.get("metadata", {}),
    ))
    mgr.activate_task_list = AsyncMock()
    mgr.load_task_list = AsyncMock(return_value=task_list)
    mgr.get_task_list_status = AsyncMock(side_effect=_get_status)
    mgr.get_next_tasks = AsyncMock(side_effect=[[task], infra_error])
    mgr.mark_task_completed = AsyncMock()

    executor = WorkflowExecutor(task_manager=mgr)

    # Inicia workflow sem esperar — apenas cria o loop em background
    info = await executor.start_workflow_execution("do stuff")
    wf_id = info["workflow_id"]

    # Aguarda o loop interno propagar o erro
    await asyncio.sleep(0.1)

    result = await executor.wait_for_workflow_completion(wf_id, timeout=timedelta(seconds=5))
    assert result.get("success") is False


@pytest.mark.unit
@pytest.mark.orchestration
async def test_start_workflow_returns_correct_total_steps():
    """AC-10a: start_workflow_execution retorna total_steps == N, não 0."""
    steps_count = 5
    task_list = _make_task_list(list_id="wf2", total=steps_count)

    mgr = MagicMock()
    mgr.create_task_list = AsyncMock(return_value=task_list)
    mgr.add_task_to_list = AsyncMock(side_effect=lambda **kw: _make_task(
        task_id="t-x", title=kw.get("title", "step"), metadata=kw.get("metadata", {}),
    ))
    mgr.activate_task_list = AsyncMock()
    mgr.load_task_list = AsyncMock(return_value=task_list)
    mgr.get_next_tasks = AsyncMock(return_value=[])
    mgr.mark_task_completed = AsyncMock()

    executor = WorkflowExecutor(task_manager=mgr)
    info = await executor.start_workflow_execution("5 step workflow")

    assert info["total_steps"] == steps_count
    assert info["execution_info"]["total_tasks"] == steps_count


@pytest.mark.unit
@pytest.mark.orchestration
async def test_start_workflow_empty_steps_returns_zero():
    """AC-10b: lista vazia → total_steps == 0 (não regride)."""
    empty_list = _make_task_list(list_id="wf3", total=0)

    mgr = MagicMock()
    mgr.create_task_list = AsyncMock(return_value=empty_list)
    mgr.add_task_to_list = AsyncMock()
    mgr.activate_task_list = AsyncMock()
    mgr.load_task_list = AsyncMock(return_value=empty_list)
    mgr.get_next_tasks = AsyncMock(return_value=[])

    executor = WorkflowExecutor(task_manager=mgr)
    info = await executor.start_workflow_execution("empty")

    assert info["total_steps"] == 0
