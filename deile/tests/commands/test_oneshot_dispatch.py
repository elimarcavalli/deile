"""Tests: issue #106 — one-shot CLI command dispatch.

Validates that every builtin command module is registered via
auto_discover_builtin_commands so /command is correctly routed in one-shot
mode instead of falling through to the LLM as natural language.

Also validates that _print_oneshot_content renders Rich content without
printing Python object repr.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from deile.commands.registry import CommandRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_COMMANDS = [
    "help",
    "debug",
    "clear",
    "status",
    "config",
    "context",
    "cost",
    "tools",
    "model",
    "export",
    "stop",
    "diff",
    "patch",
    "apply",
    "approve",
    "compact",
    "memory",
    "logs",
    "permissions",
    "pipeline",
    "pipeline-schedule",
    "plan",
    "run",
    "sandbox",
    "skills",
    "welcome",
]


# ---------------------------------------------------------------------------
# Registration completeness (root cause of #106)
# ---------------------------------------------------------------------------


class TestAllBuiltinsRegistered:
    def _make_registry(self) -> CommandRegistry:
        r = CommandRegistry()
        r.auto_discover_builtin_commands()
        return r

    @pytest.mark.parametrize("cmd_name", EXPECTED_COMMANDS)
    def test_command_is_registered(self, cmd_name):
        """Every builtin command must be discoverable after auto_discover."""
        registry = self._make_registry()
        assert registry.has_command(cmd_name), (
            f"/{cmd_name} not found in registry after auto_discover_builtin_commands. "
            "Add its module to the builtin_modules list in registry.py."
        )

    def test_pipeline_was_previously_missing(self):
        """/pipeline was the primary bug in #106 — belt-and-suspenders check."""
        registry = self._make_registry()
        assert registry.has_command("pipeline"), "/pipeline must be registered"

    def test_plan_was_previously_missing(self):
        registry = self._make_registry()
        assert registry.has_command("plan"), "/plan must be registered"

    def test_approve_was_previously_missing(self):
        registry = self._make_registry()
        assert registry.has_command("approve"), "/approve must be registered"

    def test_compact_was_previously_missing(self):
        registry = self._make_registry()
        assert registry.has_command("compact"), "/compact must be registered"

    def test_run_was_previously_missing(self):
        registry = self._make_registry()
        assert registry.has_command("run"), "/run must be registered"

    def test_welcome_was_previously_missing(self):
        registry = self._make_registry()
        assert registry.has_command("welcome"), "/welcome must be registered"


# ---------------------------------------------------------------------------
# _print_oneshot_content — Rich rendering (second bug in #106)
# ---------------------------------------------------------------------------


class TestPrintOneshotContent:
    def _capture(self, content) -> str:
        from deile.cli import _print_oneshot_content

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            _print_oneshot_content(content)
        return buf.getvalue()

    def test_plain_string_printed_as_is(self):
        out = self._capture("hello world")
        assert "hello world" in out

    def test_none_prints_nothing(self):
        out = self._capture(None)
        assert out == ""

    def test_rich_table_not_repr(self):
        from rich.table import Table

        t = Table(title="Test")
        t.add_column("Col")
        t.add_row("val")
        out = self._capture(t)
        assert "<rich.table.Table" not in out, "Should render table, not print repr"
        assert "Col" in out or "val" in out or "Test" in out

    def test_rich_panel_not_repr(self):
        from rich.panel import Panel

        p = Panel("content", title="Title")
        out = self._capture(p)
        assert "<rich.panel.Panel" not in out

    def test_list_of_tables_rendered(self):
        from rich.table import Table

        tables = []
        for i in range(3):
            t = Table(title=f"T{i}")
            t.add_column("X")
            t.add_row(f"r{i}")
            tables.append(t)

        out = self._capture(tables)
        assert "<rich.table.Table" not in out, "Should render all tables, not print list repr"
