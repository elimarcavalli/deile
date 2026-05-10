"""Unit tests for PipelineScheduleTool."""

from __future__ import annotations

from deile.tools.base import ToolContext, ToolStatus
from deile.tools.pipeline_schedule_tool import PipelineScheduleTool

# repo_git_tmp fixture is provided by conftest.py in this directory.


def _ctx(**args) -> ToolContext:
    return ToolContext(user_input="", parsed_args=args)


class TestList:
    async def test_list_empty(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        result = await tool.execute(_ctx(action="list"))
        assert result.status == ToolStatus.SUCCESS
        assert result.data["recurring"] == []
        assert result.data["oneshot"] == []


class TestAddRecurring:
    async def test_add_then_list(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(
            action="add_recurring", trigger_action="review",
            cron="*/5 * * * *", id="r1",
        ))
        assert r.status == ToolStatus.SUCCESS
        listing = await tool.execute(_ctx(action="list"))
        assert len(listing.data["recurring"]) == 1
        assert listing.data["recurring"][0]["cron"] == "*/5 * * * *"

    async def test_missing_args(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(action="add_recurring"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "MISSING_ARGS"

    async def test_invalid_cron(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(
            action="add_recurring", trigger_action="review",
            cron="totally invalid", id="r1",
        ))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "SCHEDULE_ERROR"


class TestAddOneshot:
    async def test_add_oneshot_with_target_issue(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(
            action="add_oneshot", trigger_action="implement",
            run_at="2026-05-06T18:00:00Z", target_issue=99,
        ))
        assert r.status == ToolStatus.SUCCESS
        # auto-generated id
        assert r.data["id"].startswith("oneshot-implement-")

    async def test_invalid_run_at(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(
            action="add_oneshot", trigger_action="implement",
            run_at="not a date",
        ))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "INVALID_DATETIME"


class TestRemove:
    async def test_remove_existing(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        await tool.execute(_ctx(
            action="add_recurring", trigger_action="review",
            cron="*/5 * * * *", id="r1",
        ))
        r = await tool.execute(_ctx(action="remove", id="r1"))
        assert r.status == ToolStatus.SUCCESS

    async def test_remove_missing(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(action="remove", id="nope"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "NOT_FOUND"


class TestEnableDisable:
    async def test_disable_then_enable(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        await tool.execute(_ctx(
            action="add_recurring", trigger_action="review",
            cron="*/5 * * * *", id="r1",
        ))
        r = await tool.execute(_ctx(action="disable", id="r1"))
        assert r.status == ToolStatus.SUCCESS and r.data["enabled"] is False
        r = await tool.execute(_ctx(action="enable", id="r1"))
        assert r.status == ToolStatus.SUCCESS and r.data["enabled"] is True


class TestPerMonitorIsolation:
    async def test_two_monitors_use_separate_files(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        await tool.execute(_ctx(
            action="add_recurring", trigger_action="review",
            cron="*/5 * * * *", id="alfa-loop", monitor_id="m-alfa",
        ))
        await tool.execute(_ctx(
            action="add_recurring", trigger_action="implement",
            cron="*/2 * * * *", id="beta-loop", monitor_id="m-beta",
        ))
        # Each monitor sees only its own entries.
        list_alfa = await tool.execute(_ctx(action="list", monitor_id="m-alfa"))
        list_beta = await tool.execute(_ctx(action="list", monitor_id="m-beta"))
        assert [r["id"] for r in list_alfa.data["recurring"]] == ["alfa-loop"]
        assert [r["id"] for r in list_beta.data["recurring"]] == ["beta-loop"]
        # Files exist separately.
        assert (repo_git_tmp / "config" / "pipeline_schedule_m-alfa.yaml").exists()
        assert (repo_git_tmp / "config" / "pipeline_schedule_m-beta.yaml").exists()


class TestInvalidAction:
    async def test_unknown_action(self, repo_git_tmp):
        tool = PipelineScheduleTool()
        r = await tool.execute(_ctx(action="nope"))
        assert r.status == ToolStatus.ERROR
        assert r.metadata["error_code"] == "INVALID_ACTION"
