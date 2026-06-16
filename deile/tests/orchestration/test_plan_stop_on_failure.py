"""Regression test for ``stop_on_failure`` behaviour in plan execution.

Bug: the ``break`` inside the inner ``for step in concurrent_steps:`` loop
only exited that inner loop. The outer ``while True:`` immediately called
``plan.get_next_steps()`` again and resumed executing subsequent steps,
silently ignoring ``stop_on_failure=True``. Users relying on this gate
(e.g. to halt a destructive sequence after the first failure) saw steps
keep running after a failure.

Fix: when a step fails and ``stop_on_failure`` is set, we now also flip
``self._stop_flags[plan.id] = True`` so the outer loop exits on the next
iteration.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from deile.orchestration.plan_manager import (
    ExecutionPlan,
    PlanManager,
    PlanStatus,
    PlanStep,
    StepStatus,
)
from deile.tools.base import (
    SecurityLevel,
    SyncTool,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSchema,
)


class _AlwaysFails(SyncTool):
    @property
    def name(self) -> str:
        return "always_fails"

    @property
    def description(self) -> str:
        return "deterministically fails"

    @property
    def category(self) -> str:
        return ToolCategory.OTHER.value

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="always_fails",
                description="deterministically fails",
                parameters={},
                required=[],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            )
        )

    def execute_sync(self, context: ToolContext) -> ToolResult:
        return ToolResult.error_result("boom", error_code="BOOM")


class _Records(SyncTool):
    def __init__(self, log: list) -> None:
        self._log = log
        super().__init__(
            schema=ToolSchema(
                name="records",
                description="records execution",
                parameters={"label": {"type": "string", "description": "label"}},
                required=["label"],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            )
        )

    @property
    def name(self) -> str:
        return "records"

    @property
    def description(self) -> str:
        return "records execution"

    @property
    def category(self) -> str:
        return ToolCategory.OTHER.value

    def execute_sync(self, context: ToolContext) -> ToolResult:
        self._log.append(context.parsed_args.get("label", "anon"))
        return ToolResult.success_result(data=True)


@pytest.fixture()
def plan_manager(tmp_path):
    pm = PlanManager(plans_dir=tmp_path)
    # Use an isolated registry per test so test_stop_on_failure_halts_outer_loop
    # and test_stop_on_failure_false_continues don't fight over duplicate
    # registrations of the same tool name.
    from deile.tools.registry import ToolRegistry

    pm.tool_registry = ToolRegistry()
    return pm


def _new_plan(stop_on_failure: bool, steps: list[PlanStep]) -> ExecutionPlan:
    return ExecutionPlan(
        id="p1",
        title="t",
        description="d",
        created_at=datetime.now(),
        steps=steps,
        status=PlanStatus.READY,
        max_concurrent_steps=2,
        stop_on_failure=stop_on_failure,
    )


async def test_stop_on_failure_halts_outer_loop(plan_manager) -> None:
    """A failing step in batch 1 must NOT let batch 2 run when stop_on_failure=True."""
    executed: list[str] = []
    records_tool = _Records(executed)
    fail_tool = _AlwaysFails()

    # Two batches: [s1=fail], [s2 (depends on s1)] — the dependency means
    # only s1 is in the first ready_steps list. Without the fix, the outer
    # while loop would re-query get_next_steps; but with s1 failed, s2's
    # dependency wouldn't be met, so s2 stays PENDING regardless. To exercise
    # the bug we add an independent step s3 that has no dependency and would
    # be picked up on the next outer-loop tick.
    steps = [
        PlanStep(id="s1", tool_name="always_fails", params={}, description="will fail"),
        PlanStep(
            id="s2",
            tool_name="records",
            params={"label": "second"},
            description="second batch",
        ),
    ]
    # Both s1 and s2 have no dependency; with max_concurrent_steps=1, only s1
    # is taken in batch 1, then if not stopped, s2 runs in batch 2.
    plan = _new_plan(stop_on_failure=True, steps=steps)
    plan.max_concurrent_steps = 1

    # Inject tools into registry.
    plan_manager.tool_registry.register(records_tool)
    plan_manager.tool_registry.register(fail_tool)

    # Bypass async approval / persistence side effects we don't care about.
    async def noop_save(_plan):
        return None

    plan_manager._save_plan = noop_save  # type: ignore[assignment]

    plan_manager._stop_flags[plan.id] = False
    await plan_manager._execute_plan_steps(plan, auto_approve_low_risk=True)

    # s1 should have failed; s2 must NOT have run.
    assert executed == [], f"step after failure ran despite stop_on_failure: {executed}"
    assert plan.get_step("s1").status == StepStatus.FAILED
    assert plan.get_step("s2").status == StepStatus.PENDING


async def test_stop_on_failure_false_continues(plan_manager) -> None:
    executed: list[str] = []
    records_tool = _Records(executed)
    fail_tool = _AlwaysFails()

    steps = [
        PlanStep(id="s1", tool_name="always_fails", params={}, description="will fail"),
        PlanStep(
            id="s2",
            tool_name="records",
            params={"label": "second"},
            description="second batch",
        ),
    ]
    plan = _new_plan(stop_on_failure=False, steps=steps)
    plan.max_concurrent_steps = 1

    plan_manager.tool_registry.register(records_tool)
    plan_manager.tool_registry.register(fail_tool)

    async def noop_save(_plan):
        return None

    plan_manager._save_plan = noop_save  # type: ignore[assignment]
    plan_manager._stop_flags[plan.id] = False

    await plan_manager._execute_plan_steps(plan, auto_approve_low_risk=True)

    assert executed == ["second"]
    assert plan.get_step("s1").status == StepStatus.FAILED
    assert plan.get_step("s2").status == StepStatus.COMPLETED
