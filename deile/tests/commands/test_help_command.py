"""Tests for ``/help <command>`` — the inspect.isawaitable contract fix.

``SlashCommand.get_help`` is declared ``async`` on the base class, but 17
builtin commands override it as a *synchronous* method. ``HelpCommand``
must therefore tolerate both a coroutine and a plain ``str`` — awaiting a
plain ``str`` raises ``TypeError``. These tests pin both paths so the fix
cannot be reverted silently.
"""

from __future__ import annotations

import pytest

import deile.commands.registry as _registry_mod
from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.commands.builtin.help_command import HelpCommand
from deile.commands.registry import get_command_registry
from deile.config.manager import CommandConfig


class _SyncHelpCommand(DirectCommand):
    """Command whose get_help is a synchronous override (the 17-command case)."""

    def __init__(self) -> None:
        super().__init__(CommandConfig(name="fakesync", description="sync cmd"))

    async def execute(self, context: CommandContext) -> CommandResult:
        return CommandResult.success_result("ok")

    def get_help(self) -> str:  # synchronous override — returns a plain str
        return "SYNC-HELP-MARKER"


class _AsyncHelpCommand(DirectCommand):
    """Command that keeps the base ``async def get_help`` (no override)."""

    def __init__(self) -> None:
        super().__init__(CommandConfig(name="fakeasync", description="async cmd"))

    async def execute(self, context: CommandContext) -> CommandResult:
        return CommandResult.success_result("ok")


def _purge_registry_singleton() -> None:
    _registry_mod._command_registry = None


def _renderable_text(result: CommandResult) -> str:
    """Extract the string fed into the help Panel."""
    return result.content.renderable


class TestHelpForCommand:
    def setup_method(self) -> None:
        _purge_registry_singleton()
        registry = get_command_registry()
        registry.register_command(_SyncHelpCommand())
        registry.register_command(_AsyncHelpCommand())

    def teardown_method(self) -> None:
        _purge_registry_singleton()

    @pytest.mark.unit
    async def test_help_for_command_with_sync_get_help(self):
        """A synchronous get_help override must not raise — its str is used directly."""
        ctx = CommandContext(user_input="/help fakesync", args="fakesync")
        result = await HelpCommand().execute(ctx)
        assert result.success is True
        assert "SYNC-HELP-MARKER" in _renderable_text(result)

    @pytest.mark.unit
    async def test_help_for_command_with_async_get_help(self):
        """A coroutine get_help (base class) must be awaited and resolved to str."""
        ctx = CommandContext(user_input="/help fakeasync", args="fakeasync")
        result = await HelpCommand().execute(ctx)
        assert result.success is True
        assert "fakeasync" in _renderable_text(result)

    @pytest.mark.unit
    async def test_help_for_unknown_command_returns_error(self):
        ctx = CommandContext(user_input="/help nope", args="nope")
        result = await HelpCommand().execute(ctx)
        assert result.success is False
