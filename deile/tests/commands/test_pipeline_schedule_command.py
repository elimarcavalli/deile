"""Unit tests for PipelineScheduleCommand (/pipeline-schedule slash command)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from deile.commands.base import CommandContext
from deile.commands.builtin.pipeline_schedule_command import (
    PipelineScheduleCommand, _parse_kv)
from deile.tools.base import ToolContext, ToolResult

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ctx(args: str = "") -> CommandContext:
    ctx = MagicMock(spec=CommandContext)
    ctx.args = args
    ctx.agent = MagicMock()
    return ctx


def _success(data: Any = None, message: str = "ok") -> ToolResult:
    return ToolResult.success_result(data=data, message=message)


def _error(message: str = "fail", error_code: str = "ERR") -> ToolResult:
    return ToolResult.error_result(message=message, error_code=error_code)


# ---------------------------------------------------------------------------
# _parse_kv unit tests
# ---------------------------------------------------------------------------

class TestParseKv:
    def test_single_key(self):
        assert _parse_kv("id:my_id") == {"id": "my_id"}

    def test_multiple_keys(self):
        result = _parse_kv("trigger:review cron:*/5 * * * *")
        assert result["trigger"] == "review"
        assert result["cron"] == "*/5 * * * *"

    def test_at_key(self):
        result = _parse_kv("trigger:pr_review at:2026-05-06T18:00:00Z")
        assert result["trigger"] == "pr_review"
        assert result["at"] == "2026-05-06T18:00:00Z"

    def test_empty_string(self):
        assert _parse_kv("") == {}


# ---------------------------------------------------------------------------
# sub-command: list
# ---------------------------------------------------------------------------

class TestListSubCommand:
    async def test_list_calls_tool_with_action_list(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(
            data={
                "monitor_id": "default",
                "recurring": [],
                "oneshot": [],
            },
            message="0 recurring + 0 oneshot entries",
        )
        with patch.object(cmd._tool, "execute", new=AsyncMock(return_value=tool_result)):
            result = await cmd.execute(_ctx("list"))

        assert result.success
        assert "default" in result.content

    async def test_list_with_entries_formats_output(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(
            data={
                "monitor_id": "default",
                "recurring": [{"id": "r1", "action": "review", "cron": "*/5 * * * *", "enabled": True}],
                "oneshot": [{"id": "o1", "action": "implement", "run_at": "2026-05-06T18:00:00+00:00"}],
            },
            message="1 recurring + 1 oneshot entries",
        )
        with patch.object(cmd._tool, "execute", new=AsyncMock(return_value=tool_result)):
            result = await cmd.execute(_ctx("list"))

        assert result.success
        assert "r1" in result.content
        assert "o1" in result.content


# ---------------------------------------------------------------------------
# sub-command: add-recurring
# ---------------------------------------------------------------------------

class TestAddRecurringSubCommand:
    async def test_add_recurring_delegates_to_tool(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(
            data={"id": "review_loop", "cron": "*/5 * * * *", "action": "review"},
            message="added recurring",
        )
        captured: list[dict] = []

        async def fake_execute(ctx: ToolContext) -> ToolResult:
            captured.append(dict(ctx.parsed_args))
            return tool_result

        with patch.object(cmd._tool, "execute", side_effect=fake_execute):
            result = await cmd.execute(_ctx("add-recurring trigger:review cron:*/5 * * * *"))

        assert result.success
        assert captured[0]["action"] == "add_recurring"
        assert captured[0]["trigger_action"] == "review"
        assert captured[0]["cron"] == "*/5 * * * *"

    async def test_add_recurring_missing_args_returns_error(self):
        cmd = PipelineScheduleCommand()
        result = await cmd.execute(_ctx("add-recurring trigger:review"))
        assert not result.success
        assert "cron" in result.content.lower()

    async def test_add_recurring_missing_trigger_returns_error(self):
        cmd = PipelineScheduleCommand()
        result = await cmd.execute(_ctx("add-recurring cron:*/10 * * * *"))
        assert not result.success
        assert "trigger" in result.content.lower()


# ---------------------------------------------------------------------------
# sub-command: add-oneshot
# ---------------------------------------------------------------------------

class TestAddOneshotSubCommand:
    async def test_add_oneshot_delegates_to_tool(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(
            data={"id": "o1", "run_at": "2026-05-06T18:00:00+00:00", "action": "implement"},
            message="added oneshot",
        )
        captured: list[dict] = []

        async def fake_execute(ctx: ToolContext) -> ToolResult:
            captured.append(dict(ctx.parsed_args))
            return tool_result

        with patch.object(cmd._tool, "execute", side_effect=fake_execute):
            result = await cmd.execute(
                _ctx("add-oneshot trigger:implement at:2026-05-06T18:00:00Z")
            )

        assert result.success
        assert captured[0]["action"] == "add_oneshot"
        assert captured[0]["trigger_action"] == "implement"
        assert "2026-05-06" in captured[0]["run_at"]

    async def test_add_oneshot_missing_at_returns_error(self):
        cmd = PipelineScheduleCommand()
        result = await cmd.execute(_ctx("add-oneshot trigger:review"))
        assert not result.success
        assert "at" in result.content.lower()


# ---------------------------------------------------------------------------
# sub-command: remove
# ---------------------------------------------------------------------------

class TestRemoveSubCommand:
    async def test_remove_delegates_to_tool(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(data={"id": "r1"}, message="removed r1")
        captured: list[dict] = []

        async def fake_execute(ctx: ToolContext) -> ToolResult:
            captured.append(dict(ctx.parsed_args))
            return tool_result

        with patch.object(cmd._tool, "execute", side_effect=fake_execute):
            result = await cmd.execute(_ctx("remove id:r1"))

        assert result.success
        assert captured[0] == {"action": "remove", "id": "r1"}

    async def test_remove_missing_id_returns_error(self):
        cmd = PipelineScheduleCommand()
        result = await cmd.execute(_ctx("remove"))
        assert not result.success
        assert "id" in result.content.lower()


# ---------------------------------------------------------------------------
# sub-command: enable / disable
# ---------------------------------------------------------------------------

class TestEnableDisableSubCommand:
    async def test_enable_delegates_to_tool(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(data={"id": "r1", "enabled": True}, message="r1 enabled")
        captured: list[dict] = []

        async def fake_execute(ctx: ToolContext) -> ToolResult:
            captured.append(dict(ctx.parsed_args))
            return tool_result

        with patch.object(cmd._tool, "execute", side_effect=fake_execute):
            result = await cmd.execute(_ctx("enable id:r1"))

        assert result.success
        assert captured[0] == {"action": "enable", "id": "r1"}

    async def test_disable_delegates_to_tool(self):
        cmd = PipelineScheduleCommand()
        tool_result = _success(data={"id": "r1", "enabled": False}, message="r1 disabled")
        captured: list[dict] = []

        async def fake_execute(ctx: ToolContext) -> ToolResult:
            captured.append(dict(ctx.parsed_args))
            return tool_result

        with patch.object(cmd._tool, "execute", side_effect=fake_execute):
            result = await cmd.execute(_ctx("disable id:r1"))

        assert result.success
        assert captured[0] == {"action": "disable", "id": "r1"}

    async def test_enable_missing_id_returns_error(self):
        cmd = PipelineScheduleCommand()
        result = await cmd.execute(_ctx("enable"))
        assert not result.success
        assert "id" in result.content.lower()


# ---------------------------------------------------------------------------
# invalid sub-command
# ---------------------------------------------------------------------------

class TestInvalidSubCommand:
    async def test_unknown_subcommand_returns_error(self):
        cmd = PipelineScheduleCommand()
        result = await cmd.execute(_ctx("foobar"))
        assert not result.success
        assert "unknown" in result.content.lower() or "foobar" in result.content.lower()

    async def test_empty_args_defaults_to_list(self):
        """No args → list (same as /pipeline default = status)."""
        cmd = PipelineScheduleCommand()
        tool_result = _success(
            data={"monitor_id": "default", "recurring": [], "oneshot": []},
            message="0 recurring + 0 oneshot entries",
        )
        with patch.object(cmd._tool, "execute", new=AsyncMock(return_value=tool_result)):
            result = await cmd.execute(_ctx(""))
        assert result.success


# ---------------------------------------------------------------------------
# tool error propagation
# ---------------------------------------------------------------------------

class TestToolErrorPropagation:
    async def test_tool_error_becomes_command_error(self):
        cmd = PipelineScheduleCommand()
        tool_result = _error(message="no entry with id='x'", error_code="NOT_FOUND")
        with patch.object(cmd._tool, "execute", new=AsyncMock(return_value=tool_result)):
            result = await cmd.execute(_ctx("remove id:x"))
        assert not result.success
        assert "no entry" in result.content.lower()

    async def test_tool_exception_becomes_command_error(self):
        cmd = PipelineScheduleCommand()

        async def explode(_ctx):
            raise RuntimeError("disk full")

        with patch.object(cmd._tool, "execute", side_effect=explode):
            result = await cmd.execute(_ctx("list"))

        assert not result.success
        assert "RuntimeError" in result.content or "disk full" in result.content
