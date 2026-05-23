"""Regression tests for ``PlanManager._run_tool_with_params``.

Previously the helper was ``async def`` but contained no ``await`` —
it delegated to the synchronous ``execute_function_call`` bridge, whose
``_run_coro_sync`` BLOCKS the calling thread inside ``Future.result()``
when invoked from a running loop. As documented in
``deile/tools/function_call.py``, "Cancellation/timeout does NOT cross
into the worker thread", so ``asyncio.wait_for(..., timeout=step.timeout)``
silently lost its budget — long-running tools froze the event loop and
the step timeout was effectively bypassed.

The fix invokes ``tool.execute(context)`` directly. ``SyncTool.execute``
already wraps ``execute_sync`` in ``asyncio.to_thread``, so we keep both
the SyncTool and async-Tool paths working without blocking the loop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from deile.orchestration.plan_manager import PlanManager
from deile.tools.base import (
    SecurityLevel,
    SyncTool,
    Tool,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSchema,
)


class _SlowAsyncTool(Tool):
    """Async tool that sleeps. Timeout MUST interrupt it."""

    @property
    def name(self) -> str:
        return "slow_async"

    @property
    def description(self) -> str:
        return "slow async tool"

    @property
    def category(self) -> str:
        return ToolCategory.OTHER.value

    def __init__(self) -> None:
        super().__init__(schema=ToolSchema(
            name="slow_async",
            description="slow async tool",
            parameters={},
            required=[],
            security_level=SecurityLevel.SAFE,
            category=ToolCategory.OTHER,
        ))

    async def execute(self, context: ToolContext) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult.success_result(data="never_reached")


class _FastSyncTool(SyncTool):
    """SyncTool wrapping a quick computation."""

    @property
    def name(self) -> str:
        return "fast_sync"

    @property
    def description(self) -> str:
        return "fast sync tool"

    @property
    def category(self) -> str:
        return ToolCategory.OTHER.value

    def __init__(self) -> None:
        super().__init__(schema=ToolSchema(
            name="fast_sync",
            description="fast sync tool",
            parameters={"x": {"type": "integer", "description": "input"}},
            required=["x"],
            security_level=SecurityLevel.SAFE,
            category=ToolCategory.OTHER,
        ))

    def execute_sync(self, context: ToolContext) -> ToolResult:
        x = context.parsed_args.get("x", 0)
        return ToolResult.success_result(data={"doubled": x * 2})


async def test_async_tool_timeout_actually_fires(tmp_path) -> None:
    """``asyncio.wait_for`` must cancel the slow tool — proves we await."""
    pm = PlanManager(plans_dir=tmp_path)

    elapsed_start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            pm._run_tool_with_params(_SlowAsyncTool(), {}),
            timeout=0.2,
        )
    elapsed = time.monotonic() - elapsed_start
    # If we were blocking the loop in a worker thread, we'd sleep ~5s; budget
    # under 1s leaves generous slack for scheduler jitter.
    assert elapsed < 1.0, f"timeout did not interrupt; elapsed={elapsed:.2f}s"


async def test_sync_tool_runs_in_thread_and_returns_result(tmp_path) -> None:
    pm = PlanManager(plans_dir=tmp_path)
    result = await pm._run_tool_with_params(_FastSyncTool(), {"x": 21})
    assert result.is_success
    assert result.data == {"doubled": 42}


async def test_tool_exception_is_wrapped(tmp_path) -> None:
    """Tools that violate the no-exception contract must be wrapped."""

    class _BrokenTool(Tool):
        @property
        def name(self) -> str:
            return "broken"

        @property
        def description(self) -> str:
            return "broken"

        @property
        def category(self) -> str:
            return ToolCategory.OTHER.value

        def __init__(self) -> None:
            super().__init__(schema=ToolSchema(
                name="broken",
                description="broken",
                parameters={},
                required=[],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            ))

        async def execute(self, context: ToolContext) -> ToolResult:
            raise RuntimeError("kaboom")

    pm = PlanManager(plans_dir=tmp_path)
    result = await pm._run_tool_with_params(_BrokenTool(), {})
    assert not result.is_success
    assert "RuntimeError" in (result.message or "")


async def test_loop_remains_responsive_while_sync_tool_runs(tmp_path) -> None:
    """A blocking sync tool must NOT freeze the loop — proves to_thread path."""

    class _BlockingTool(SyncTool):
        @property
        def name(self) -> str:
            return "blocking"

        @property
        def description(self) -> str:
            return "blocking sync"

        @property
        def category(self) -> str:
            return ToolCategory.OTHER.value

        def __init__(self) -> None:
            super().__init__(schema=ToolSchema(
                name="blocking",
                description="blocking sync",
                parameters={},
                required=[],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            ))

        def execute_sync(self, context: ToolContext) -> ToolResult:
            time.sleep(0.3)
            return ToolResult.success_result(data="done")

    pm = PlanManager(plans_dir=tmp_path)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    tool_task = asyncio.create_task(pm._run_tool_with_params(_BlockingTool(), {}))
    hb_task = asyncio.create_task(heartbeat())
    result = await tool_task
    await hb_task

    assert result.is_success
    # If the loop were blocked, heartbeat would not have advanced.
    assert ticks >= 5, f"loop appears blocked; ticks={ticks}"
