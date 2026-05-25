"""Regression test for ``_execute_tool_action`` returning ``ToolResult.data``.

Bug (same family as the plan_manager fix): ``workflow_executor`` did
``return result.output`` where ``result`` is a ``ToolResult`` — which has
NO ``output`` field (data lives in ``.data``). Any path that returned
from ``_execute_tool_action`` would crash with ``AttributeError``. The
existing tests only exercised the failure path (where the function
raises BEFORE reaching the return), so the bug never surfaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.workflow_executor import WorkflowExecutor
from deile.tools.base import ToolResult, ToolStatus


def _make_executor():
    mock_registry = MagicMock()
    mock_registry.get_enabled = MagicMock(return_value=MagicMock())
    mock_registry.execute_tool = AsyncMock()
    task_mgr = MagicMock()
    task_mgr.create_task = AsyncMock(return_value=MagicMock(id="t1"))
    task_mgr.update_task = AsyncMock()
    with patch("deile.orchestration.workflow_executor.get_tool_registry",
               return_value=mock_registry):
        executor = WorkflowExecutor(task_manager=task_mgr)
    executor._mock_registry = mock_registry
    return executor


@pytest.mark.unit
async def test_execute_tool_action_returns_tool_result_data() -> None:
    executor = _make_executor()
    payload = {"answer": 42}
    success_result = ToolResult(status=ToolStatus.SUCCESS, data=payload, message="ok")
    executor._mock_registry.execute_tool.return_value = success_result

    fake_task = MagicMock(id="t1", input_data={})
    out = await executor._execute_tool_action(
        action_name="my_tool", params={"k": "v"}, task=fake_task
    )
    # Before the fix this would have raised ``AttributeError: 'ToolResult'
    # object has no attribute 'output'``.
    assert out == payload
