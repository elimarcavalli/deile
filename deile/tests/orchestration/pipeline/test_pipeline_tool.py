"""Unit tests for PipelineTool — LLM-callable pipeline interface."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import GhCommandError, IssueRef
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.tools.base import ToolContext, ToolStatus
from deile.tools.pipeline_tool import PipelineTool


def _make_context(action: str, agent=None) -> ToolContext:
    ctx = ToolContext(user_input="", parsed_args={"action": action})
    if agent is not None:
        ctx.session_data["agent"] = agent
    return ctx


def _make_monitor() -> PipelineMonitor:
    cfg = PipelineConfig(repo="o/n", base_repo_path=Path("/tmp"))
    # Inject mocks for github + worktrees so PipelineMonitor.__init__ doesn't
    # validate /tmp as a git repo.
    monitor = PipelineMonitor(cfg, github=MagicMock(), worktrees=MagicMock())
    monitor.start = AsyncMock()
    monitor.stop = AsyncMock()
    monitor.tick = AsyncMock()
    return monitor


class TestPipelineToolSchema:
    def test_schema_metadata(self):
        tool = PipelineTool()
        assert tool.name == "pipeline"
        assert tool.category == "system"
        assert "start" in tool.description.lower()
        schema = tool._schema
        # JSON Schema: parameters MUST be a wrapped object schema for the
        # OpenAI/Anthropic/Gemini function-calling APIs.
        assert schema.parameters["type"] == "object"
        properties = schema.parameters["properties"]
        assert "action" in properties
        assert "start" in properties["action"]["enum"]
        assert "stop" in properties["action"]["enum"]
        assert "status" in properties["action"]["enum"]
        assert "tick" in properties["action"]["enum"]
        assert "reset" in properties["action"]["enum"]
        # And the ToolSchema serializes correctly for OpenAI function calling.
        fn = schema.to_openai_function()
        assert fn["function"]["parameters"]["type"] == "object"


class TestPipelineToolActions:
    async def test_invalid_action_returns_error(self):
        tool = PipelineTool()
        ctx = _make_context("nope")
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.ERROR
        assert "INVALID_ACTION" in result.metadata.get("error_code", "")

    async def test_start_calls_monitor_start(self):
        tool = PipelineTool()
        agent = MagicMock()
        agent.pipeline_monitor = _make_monitor()
        ctx = _make_context("start", agent=agent)
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.SUCCESS
        agent.pipeline_monitor.start.assert_awaited_once()
        assert result.data["running"] is True

    async def test_stop_calls_monitor_stop(self):
        tool = PipelineTool()
        agent = MagicMock()
        agent.pipeline_monitor = _make_monitor()
        ctx = _make_context("stop", agent=agent)
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.SUCCESS
        agent.pipeline_monitor.stop.assert_awaited_once()
        assert result.data["running"] is False

    async def test_tick_calls_monitor_tick_and_returns_stats(self):
        tool = PipelineTool()
        agent = MagicMock()
        agent.pipeline_monitor = _make_monitor()
        ctx = _make_context("tick", agent=agent)
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.SUCCESS
        agent.pipeline_monitor.tick.assert_awaited_once()
        assert "ticks" in result.data
        assert "errors" in result.data

    async def test_status_default_action_when_args_missing(self):
        tool = PipelineTool()
        agent = MagicMock()
        agent.pipeline_monitor = _make_monitor()
        ctx = ToolContext(user_input="", parsed_args={})
        ctx.session_data["agent"] = agent
        result = await tool.execute(ctx)
        # Default action is "status" (idempotent, never starts/stops the monitor).
        assert result.status == ToolStatus.SUCCESS
        agent.pipeline_monitor.start.assert_not_called()
        agent.pipeline_monitor.stop.assert_not_called()
        assert "running" in result.data

    async def test_status_returns_running_flag(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        # Simulate a running task.
        monitor._task = MagicMock()
        monitor._task.done.return_value = False
        agent.pipeline_monitor = monitor
        ctx = _make_context("status", agent=agent)
        result = await tool.execute(ctx)
        assert result.data["running"] is True

    async def test_status_running_false_when_task_done(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        monitor._task = MagicMock()
        monitor._task.done.return_value = True
        agent.pipeline_monitor = monitor
        ctx = _make_context("status", agent=agent)
        result = await tool.execute(ctx)
        assert result.data["running"] is False


class TestPipelineToolMonitorReuse:
    async def test_reuses_agent_pipeline_monitor(self):
        tool = PipelineTool()
        existing = _make_monitor()
        agent = MagicMock()
        agent.pipeline_monitor = existing
        ctx = _make_context("status", agent=agent)
        await tool.execute(ctx)
        # The same instance was reused — no new monitor was attached to the agent.
        assert agent.pipeline_monitor is existing

    async def test_creates_monitor_when_agent_has_none(self, repo_git_tmp):
        # The fresh-monitor path goes through WorktreeManager which validates
        # that base_repo_path is a real git repo. repo_git_tmp provides one
        # inside the git repo root (safe root) with DEILE_PIPELINE_BASE_PATH set.
        tool = PipelineTool()
        agent = MagicMock(spec=["pipeline_monitor"])
        agent.pipeline_monitor = None
        ctx = _make_context("status", agent=agent)
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.SUCCESS
        # The tool tried to attach a fresh monitor to the agent.
        assert agent.pipeline_monitor is not None

    async def test_works_without_agent(self, repo_git_tmp):
        tool = PipelineTool()
        ctx = _make_context("status")  # no agent
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.SUCCESS


class TestPipelineToolFailureHandling:
    async def test_monitor_exception_returns_error_result(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        monitor.tick = AsyncMock(side_effect=RuntimeError("kaboom"))
        agent.pipeline_monitor = monitor
        ctx = _make_context("tick", agent=agent)
        result = await tool.execute(ctx)
        assert result.status == ToolStatus.ERROR
        assert "kaboom" in result.message
        assert result.metadata.get("error_code") == "PIPELINE_OP_FAILED"


class TestPipelineToolReset:
    """action='reset' — gap #34 lock-label removal via the LLM-callable tool."""

    @staticmethod
    def _reset_context(target, agent) -> ToolContext:
        ctx = ToolContext(
            user_input="", parsed_args={"action": "reset", "target": target}
        )
        ctx.session_data["agent"] = agent
        return ctx

    async def test_reset_removes_lock_labels(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        issue = IssueRef(
            number=7, title="t", url="u",
            labels=("intent", "~batch:abc12345", "~by:default"),
        )
        monitor.github.get_issue = AsyncMock(return_value=issue)
        monitor.github.remove_labels = AsyncMock()
        agent.pipeline_monitor = monitor
        result = await tool.execute(self._reset_context(7, agent))
        assert result.status == ToolStatus.SUCCESS
        assert result.data["issue"] == 7
        assert "unlocked" in result.message
        monitor.github.remove_labels.assert_awaited_once()

    async def test_reset_noop_when_no_lock_labels(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        issue = IssueRef(number=8, title="t", url="u", labels=("intent", "bug"))
        monitor.github.get_issue = AsyncMock(return_value=issue)
        monitor.github.remove_labels = AsyncMock()
        agent.pipeline_monitor = monitor
        result = await tool.execute(self._reset_context(8, agent))
        assert result.status == ToolStatus.SUCCESS
        assert "no lock labels" in result.message
        monitor.github.remove_labels.assert_not_called()

    async def test_reset_missing_target_returns_error(self):
        tool = PipelineTool()
        agent = MagicMock()
        agent.pipeline_monitor = _make_monitor()
        result = await tool.execute(self._reset_context(None, agent))
        assert result.status == ToolStatus.ERROR
        assert result.metadata.get("error_code") == "MISSING_TARGET"

    async def test_reset_invalid_target_returns_error(self):
        tool = PipelineTool()
        agent = MagicMock()
        agent.pipeline_monitor = _make_monitor()
        result = await tool.execute(self._reset_context("abc", agent))
        assert result.status == ToolStatus.ERROR
        assert result.metadata.get("error_code") == "INVALID_TARGET"

    async def test_reset_surfaces_gh_fetch_failure(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        monitor.github.get_issue = AsyncMock(
            side_effect=GhCommandError(["gh", "issue", "view"], 1, "", "not found")
        )
        agent.pipeline_monitor = monitor
        result = await tool.execute(self._reset_context(9, agent))
        assert result.status == ToolStatus.SUCCESS
        assert "issue #9" in result.message
        assert "gh error fetching issue" in result.message

    async def test_reset_surfaces_remove_labels_failure(self):
        tool = PipelineTool()
        agent = MagicMock()
        monitor = _make_monitor()
        issue = IssueRef(number=10, title="t", url="u", labels=("~batch:abc12345",))
        monitor.github.get_issue = AsyncMock(return_value=issue)
        monitor.github.remove_labels = AsyncMock(
            side_effect=GhCommandError(["gh", "issue", "edit"], 1, "", "boom")
        )
        agent.pipeline_monitor = monitor
        result = await tool.execute(self._reset_context(10, agent))
        assert result.status == ToolStatus.SUCCESS
        assert "failed to remove labels" in result.message


class TestAutoDiscover:
    def test_pipeline_tool_in_default_packages(self):
        # Construct a registry and inspect the default discovery list.
        # We don't actually import; just check the constant.
        import inspect

        from deile.tools.registry import ToolRegistry
        src = inspect.getsource(ToolRegistry.auto_discover)
        assert "deile.tools.pipeline_tool" in src
