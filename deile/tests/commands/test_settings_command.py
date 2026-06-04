"""Tests: /settings slash command suite (issue #357).

Covers set, get, list, unset, where subcommands plus aliases (ls, rm)
and the blocked-secret / project-trust flows.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from deile.commands.base import CommandContext, CommandResult
from deile.commands.builtin.settings_command import SettingsCommand
from deile.commands.settings_manager import SettingsManager

pytestmark = pytest.mark.usefixtures("allow_settings_writes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(
        project_dir=tmp_path / "project",
        user_home=tmp_path / "home",
    )


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/settings {args}", args=args)


async def _run(cmd: SettingsCommand, args: str) -> CommandResult:
    return await cmd.execute(_ctx(args))


# ---------------------------------------------------------------------------
# Fixture: command backed by isolated SettingsManager
# ---------------------------------------------------------------------------


@pytest.fixture
def cmd_with_tmp(tmp_path):
    """Return (SettingsCommand, SettingsManager) backed by tmp_path."""
    mgr = _make_manager(tmp_path)
    cmd = SettingsCommand()
    return cmd, mgr


# ---------------------------------------------------------------------------
# No-args / help
# ---------------------------------------------------------------------------


class TestHelp:
    async def test_no_args_returns_help(self):
        result = await _run(SettingsCommand(), "")
        assert result.success
        assert "set" in result.content
        assert "get" in result.content
        assert "list" in result.content
        assert "unset" in result.content
        assert "where" in result.content

    async def test_unknown_subcommand_returns_error(self):
        result = await _run(SettingsCommand(), "foobar")
        assert not result.success
        assert "Unknown subcommand" in result.content

    async def test_help_flag(self):
        result = await _run(SettingsCommand(), "--help")
        assert result.success


# ---------------------------------------------------------------------------
# /settings set
# ---------------------------------------------------------------------------


class TestSet:
    async def test_set_writes_value(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "set pipeline.poll_interval 120")
        assert result.success, result.content
        assert mgr.get_setting("pipeline.poll_interval") == 120

    async def test_set_coerces_bool(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "set ui.streaming_enabled true")
        assert result.success, result.content
        assert mgr.get_setting("ui.streaming_enabled") is True

    async def test_set_missing_value_returns_error(self):
        result = await _run(SettingsCommand(), "set pipeline.poll_interval")
        assert not result.success
        assert "Usage" in result.content

    async def test_set_missing_key_and_value_returns_error(self):
        result = await _run(SettingsCommand(), "set")
        assert not result.success

    async def test_set_blocked_secret_key(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "set ANTHROPIC_API_KEY sk-test-abc")
        assert not result.success

    async def test_set_project_scope_without_trust_returns_error(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "set pipeline.poll_interval 60 --scope=project")
        assert not result.success
        assert "trust" in result.content.lower() or "not trusted" in result.content.lower()

    async def test_set_scope_user_alias(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "set pipeline.poll_interval 99 --scope=user")
        assert result.success, result.content
        assert mgr.get_setting("pipeline.poll_interval") == 99


# ---------------------------------------------------------------------------
# /settings get
# ---------------------------------------------------------------------------


class TestGet:
    async def test_get_returns_set_value(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("pipeline.poll_interval", 77)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "get pipeline.poll_interval")
        assert result.success
        # content is a Rich Table — check it serialises to something containing 77
        from io import StringIO

        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=120)
        c.print(result.content)
        text = buf.getvalue()
        assert "77" in text

    async def test_get_unknown_key_shows_not_set(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "get totally.unknown.key.xyz")
        assert result.success

    async def test_get_missing_key_returns_error(self):
        result = await _run(SettingsCommand(), "get")
        assert not result.success


# ---------------------------------------------------------------------------
# /settings list
# ---------------------------------------------------------------------------


class TestList:
    async def test_list_returns_table(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "list")
        assert result.success

    async def test_list_pipeline_prefix_filters(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "list pipeline")
        assert result.success
        from io import StringIO

        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=200)
        c.print(result.content)
        text = buf.getvalue()
        assert "pipeline" in text

    async def test_list_alias_ls(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "ls pipeline")
        assert result.success

    async def test_list_nonexistent_prefix(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "list zzznonexistent")
        assert result.success
        assert "No settings" in result.content

    async def test_list_shows_pipeline_keys(self, tmp_path):
        """pipeline.poll_interval must appear in list output."""
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "list pipeline")
        assert result.success
        from io import StringIO

        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=200)
        c.print(result.content)
        text = buf.getvalue()
        assert "pipeline.poll_interval" in text


# ---------------------------------------------------------------------------
# /settings unset
# ---------------------------------------------------------------------------


class TestUnset:
    async def test_unset_removes_key(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("pipeline.poll_interval", 55)
        assert mgr.get_setting("pipeline.poll_interval") == 55

        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "unset pipeline.poll_interval")
        assert result.success, result.content
        assert mgr.get_setting("pipeline.poll_interval") is None

    async def test_unset_absent_key_is_success(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "unset pipeline.poll_interval")
        assert result.success

    async def test_unset_alias_rm(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("pipeline.poll_interval", 42)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "rm pipeline.poll_interval")
        assert result.success
        assert mgr.get_setting("pipeline.poll_interval") is None

    async def test_unset_missing_key_returns_error(self):
        result = await _run(SettingsCommand(), "unset")
        assert not result.success

    async def test_unset_blocked_secret_key(self, tmp_path):
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "unset ANTHROPIC_API_KEY")
        assert not result.success


# ---------------------------------------------------------------------------
# /settings where
# ---------------------------------------------------------------------------


class TestWhere:
    async def test_where_shows_all_layers(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("pipeline.poll_interval", 120)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "where pipeline.poll_interval")
        assert result.success
        from io import StringIO

        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=200)
        c.print(result.content)
        text = buf.getvalue()
        assert "default" in text
        assert "user" in text
        assert "project" in text
        assert "env" in text

    async def test_where_shows_winning_layer(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("pipeline.poll_interval", 120)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "where pipeline.poll_interval")
        assert result.success
        from io import StringIO

        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=200)
        c.print(result.content)
        text = buf.getvalue()
        assert "wins" in text.lower() or "←" in text or "120" in text

    async def test_where_missing_key_returns_error(self):
        result = await _run(SettingsCommand(), "where")
        assert not result.success

    async def test_where_env_detected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_PREFERRED_MODEL", "anthropic:claude-sonnet-4-6")
        mgr = _make_manager(tmp_path)
        cmd = SettingsCommand()
        with patch(
            "deile.commands.builtin.settings_command.SettingsManager",
            return_value=mgr,
        ):
            result = await _run(cmd, "where model.preferred")
        assert result.success
        from io import StringIO

        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=200)
        c.print(result.content)
        text = buf.getvalue()
        assert "DEILE_PREFERRED_MODEL" in text


# ---------------------------------------------------------------------------
# SettingsManager.unset_setting (unit tests for the new method)
# ---------------------------------------------------------------------------


class TestUnsetSettingMethod:
    def test_unset_removes_nested_key(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("pipeline.poll_interval", 99)
        assert mgr.get_setting("pipeline.poll_interval") == 99
        result = mgr.unset_setting("pipeline.poll_interval")
        assert result is True
        assert mgr.get_setting("pipeline.poll_interval") is None

    def test_unset_absent_key_returns_true(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.unset_setting("pipeline.poll_interval")
        assert result is True

    def test_unset_secret_key_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.unset_setting("ANTHROPIC_API_KEY")
        assert result is False

    def test_unset_invalid_scope_raises(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.unset_setting("pipeline.poll_interval", scope="badscope")

    def test_unset_top_level_key(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("debug", True)
        result = mgr.unset_setting("debug")
        assert result is True
        assert mgr.get_setting("debug") is None
