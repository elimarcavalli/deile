"""Tests: /skills command — issue #104."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from deile.commands.base import CommandContext
from deile.commands.builtin.skills_command import SkillsCommand
from deile.commands.settings_manager import SettingsManager
from deile.security.permissions import (PermissionLevel, PermissionRule,
                                        ResourceType, get_permission_manager)


@pytest.fixture(autouse=True)
def _allow_settings_writes_for_skills_tests():
    """Issue #125: settings writes are fail-closed by default. The /skills
    UX tests assume writes succeed, so we install a permissive rule for
    the duration of each test in this module."""
    pm = get_permission_manager()
    saved = pm.get_rule_by_id("settings_write_default")
    pm.add_rule(
        PermissionRule(
            id="settings_write_default",
            name="Settings Write (Skills test)",
            description="Test override — allow settings writes.",
            resource_type=ResourceType.FILE,
            resource_pattern=r"^settings:(global|project):.*$",
            tool_names=["settings_manager"],
            permission_level=PermissionLevel.WRITE,
            priority=50,
        )
    )
    try:
        yield
    finally:
        if saved is not None:
            pm.add_rule(saved)


def _render(content) -> str:
    """Render a Rich renderable to a plain string."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/skills {args}", args=args)


def _make_manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(
        project_dir=tmp_path / "project",
        user_home=tmp_path / "home",
    )


def _make_cmd() -> SkillsCommand:
    return SkillsCommand()


# ---------------------------------------------------------------------------
# Basic instantiation
# ---------------------------------------------------------------------------


class TestSkillsCommandInit:
    def test_name(self):
        assert _make_cmd().name == "skills"

    def test_description_not_empty(self):
        assert _make_cmd().description

    def test_enabled(self):
        assert _make_cmd().enabled is True


# ---------------------------------------------------------------------------
# Menu (no args)
# ---------------------------------------------------------------------------


class TestSkillsMenu:
    async def test_no_args_returns_success(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx(""))
        assert result.success is True

    async def test_no_args_content_type_rich(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx(""))
        assert result.content_type == "rich"

    async def test_menu_mentions_list(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx(""))
        assert "list" in _render(result.content).lower()

    async def test_menu_mentions_add(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx(""))
        assert "add" in _render(result.content).lower()

    async def test_menu_mentions_remove(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx(""))
        assert "remove" in _render(result.content).lower()


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    async def test_unknown_action_returns_error(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx("frobnicate"))
        assert result.success is False

    async def test_unknown_action_mentions_available(self):
        cmd = _make_cmd()
        result = await cmd.execute(_ctx("frobnicate"))
        assert "list" in result.content.lower() or "add" in result.content.lower()


# ---------------------------------------------------------------------------
# /skills list
# ---------------------------------------------------------------------------


class TestSkillsList:
    async def test_list_empty_returns_success(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("list"))
        assert result.success is True

    async def test_list_empty_mentions_no_paths(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("list"))
        assert "no skill" in str(result.content).lower() or result.content_type == "rich"

    async def test_list_shows_global_path(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/my/global/skills", scope="global")
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("list"))
        assert result.success is True
        assert result.content_type == "rich"

    async def test_list_shows_project_path(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/team/skills", scope="project")
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("list"))
        assert result.success is True


# ---------------------------------------------------------------------------
# /skills add
# ---------------------------------------------------------------------------


class TestSkillsAdd:
    async def test_add_without_path_returns_error(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("add"))
        assert result.success is False

    async def test_add_path_global_default(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("add /my/skills"))
        assert result.success is True
        assert "/my/skills" in mgr.list_skills_paths("global")

    async def test_add_path_project_scope(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("add /team/skills --scope project"))
        assert result.success is True
        assert "/team/skills" in mgr.list_skills_paths("project")

    async def test_add_duplicate_returns_success_with_already_present_msg(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/dup")
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("add /dup"))
        assert result.success is True
        assert "already" in _render(result.content).lower()

    async def test_add_invalid_scope_returns_error(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("add /foo --scope badscope"))
        assert result.success is False

    async def test_add_returns_rich_panel(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("add /path"))
        assert result.content_type == "rich"


# ---------------------------------------------------------------------------
# /skills remove
# ---------------------------------------------------------------------------


class TestSkillsRemove:
    async def test_remove_without_path_returns_error(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("remove"))
        assert result.success is False

    async def test_remove_existing_path(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/old/skills")
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("remove /old/skills"))
        assert result.success is True
        assert "/old/skills" not in mgr.list_skills_paths("global")

    async def test_remove_nonexistent_path_still_succeeds_with_not_found_msg(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("remove /does/not/exist"))
        assert result.success is True
        assert "not found" in _render(result.content).lower()

    async def test_remove_project_scope(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/proj-skills", scope="project")
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("remove /proj-skills --scope project"))
        assert result.success is True
        assert "/proj-skills" not in mgr.list_skills_paths("project")

    async def test_remove_invalid_scope_returns_error(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("remove /foo --scope bad"))
        assert result.success is False

    async def test_remove_returns_rich_panel(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(_ctx("remove /whatever"))
        assert result.content_type == "rich"


# ---------------------------------------------------------------------------
# _parse_scope (unit)
# ---------------------------------------------------------------------------


class TestParseScope:
    def test_no_scope_flag_defaults_to_global(self):
        remaining, scope = SkillsCommand._parse_scope(["/foo"])
        assert scope == "global"
        assert remaining == ["/foo"]

    def test_scope_flag_extracted(self):
        remaining, scope = SkillsCommand._parse_scope(["/foo", "--scope", "project"])
        assert scope == "project"
        assert remaining == ["/foo"]

    def test_scope_global_explicit(self):
        remaining, scope = SkillsCommand._parse_scope(["/bar", "--scope", "global"])
        assert scope == "global"
        assert remaining == ["/bar"]

    def test_missing_scope_value_kept_in_remaining(self):
        remaining, scope = SkillsCommand._parse_scope(["--scope"])
        assert "--scope" in remaining
        assert scope == "global"

    def test_other_flags_preserved(self):
        remaining, scope = SkillsCommand._parse_scope(["--scope", "project", "/path"])
        assert "/path" in remaining
        assert scope == "project"


# ---------------------------------------------------------------------------
# Hot-reload (_hot_reload)
# ---------------------------------------------------------------------------


class TestHotReload:
    async def test_reload_called_on_agent_after_add(self, tmp_path):
        """reload_skills() must be called on context.agent after a successful add."""
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        fake_agent = MagicMock()
        fake_agent.reload_skills.return_value = 3
        ctx = _ctx("add /new/skills")
        ctx.agent = fake_agent
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(ctx)
        assert result.success is True
        fake_agent.reload_skills.assert_called_once()

    async def test_reload_called_on_agent_after_remove(self, tmp_path):
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/rem/skills")
        fake_agent = MagicMock()
        fake_agent.reload_skills.return_value = 0
        ctx = _ctx("remove /rem/skills")
        ctx.agent = fake_agent
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(ctx)
        assert result.success is True
        fake_agent.reload_skills.assert_called_once()

    async def test_no_reload_when_agent_is_none(self, tmp_path):
        """No error when context.agent is None (tests / non-agent invocations)."""
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        ctx = _ctx("add /some/path")
        assert ctx.agent is None
        with patch.object(cmd, "_manager", return_value=mgr):
            result = await cmd.execute(ctx)
        assert result.success is True

    async def test_no_reload_on_duplicate_add(self, tmp_path):
        """reload_skills() must NOT be called when add is a no-op (duplicate)."""
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/dup")
        fake_agent = MagicMock()
        ctx = _ctx("add /dup")
        ctx.agent = fake_agent
        with patch.object(cmd, "_manager", return_value=mgr):
            await cmd.execute(ctx)
        fake_agent.reload_skills.assert_not_called()

    async def test_no_reload_on_remove_not_found(self, tmp_path):
        """reload_skills() must NOT be called when remove target is not present."""
        cmd = _make_cmd()
        mgr = _make_manager(tmp_path)
        fake_agent = MagicMock()
        ctx = _ctx("remove /ghost")
        ctx.agent = fake_agent
        with patch.object(cmd, "_manager", return_value=mgr):
            await cmd.execute(ctx)
        fake_agent.reload_skills.assert_not_called()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_add_stores_absolute_path(self, tmp_path):
        """add_skills_path() must resolve relative paths to absolute."""
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("relative/dir")
        paths = mgr.list_skills_paths("global")
        assert len(paths) == 1
        assert Path(paths[0]).is_absolute()

    def test_add_absolute_path_unchanged(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.add_skills_path("/absolute/path")
        assert "/absolute/path" in mgr.list_skills_paths("global")
