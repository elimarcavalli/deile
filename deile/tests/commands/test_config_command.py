"""Tests: /config command — show_config action.

Regression guard for the bug where show_config returned a bare list of
Rich Table objects instead of a single renderable, causing:

    Unable to render [<rich.table.Table …>, …]; A str, Segment or object
    with __rich_console__ method is required
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from deile.commands.actions import CommandActions
from deile.commands.base import CommandContext
from deile.config.manager import CommandConfig, DeileConfig, GeminiConfig, SystemConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    """Render a Rich renderable to plain text. Raises if content is not renderable."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=120)
    console.print(content)
    return buf.getvalue()


def _make_config(commands: dict | None = None) -> DeileConfig:
    cfg = DeileConfig()
    cfg.system = SystemConfig(debug_mode=False, log_level="INFO", log_requests=False, log_responses=False)
    cfg.gemini = GeminiConfig()
    cfg.commands = commands or {
        "help": CommandConfig(name="help", description="Show help", action="show_help"),
        "config": CommandConfig(name="config", description="Show config", action="show_config"),
    }
    return cfg


def _make_config_manager(cfg: DeileConfig | None = None) -> MagicMock:
    mgr = MagicMock()
    mgr.get_config.return_value = cfg or _make_config()
    return mgr


def _ctx() -> CommandContext:
    return CommandContext(user_input="/config", args="")


# ---------------------------------------------------------------------------
# show_config — basic contract
# ---------------------------------------------------------------------------


class TestShowConfigResult:
    async def test_returns_success(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert result.success is True

    async def test_content_type_is_rich(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert result.content_type == "rich"

    async def test_content_is_single_renderable_not_list(self):
        """The fix: content must NOT be a bare list."""
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert not isinstance(result.content, list), (
            "content must be a single Rich renderable, not a list"
        )

    async def test_content_renders_without_error(self):
        """Rich must be able to render the content directly (the original crash scenario)."""
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        rendered = _render(result.content)
        assert rendered  # non-empty

    async def test_metadata_has_config_sections(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert result.metadata.get("config_sections") == ["system", "gemini", "commands"]


# ---------------------------------------------------------------------------
# show_config — content coverage
# ---------------------------------------------------------------------------


class TestShowConfigContent:
    async def test_rendered_output_mentions_system(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert "system" in _render(result.content).lower()

    async def test_rendered_output_mentions_gemini(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert "gemini" in _render(result.content).lower()

    async def test_rendered_output_mentions_commands(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        assert "command" in _render(result.content).lower()

    async def test_debug_mode_reflected_in_output(self):
        cfg = _make_config()
        cfg.system.debug_mode = True
        actions = CommandActions(config_manager=_make_config_manager(cfg))
        result = await actions.show_config("", _ctx())
        assert "enabled" in _render(result.content).lower()

    async def test_command_names_appear_in_output(self):
        actions = CommandActions(config_manager=_make_config_manager())
        result = await actions.show_config("", _ctx())
        rendered = _render(result.content)
        assert "/help" in rendered or "help" in rendered

    async def test_empty_commands_dict_does_not_crash(self):
        cfg = _make_config(commands={})
        actions = CommandActions(config_manager=_make_config_manager(cfg))
        result = await actions.show_config("", _ctx())
        assert result.success is True
        _render(result.content)  # must not raise


# ---------------------------------------------------------------------------
# show_config — error path
# ---------------------------------------------------------------------------


class TestShowConfigErrors:
    async def test_no_config_manager_returns_error(self):
        actions = CommandActions(config_manager=None)
        result = await actions.show_config("", _ctx())
        assert result.success is False

    async def test_config_manager_raises_returns_error(self):
        mgr = MagicMock()
        mgr.get_config.side_effect = RuntimeError("boom")
        actions = CommandActions(config_manager=mgr)
        result = await actions.show_config("", _ctx())
        assert result.success is False
