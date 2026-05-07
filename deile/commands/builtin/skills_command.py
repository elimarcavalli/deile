"""Skills management command (/skills) — issue #104.

Provides a text-based menu to list, add and remove skill directories
from the global (~/.deile/settings.json) and project (.deile/settings.json)
settings layers managed by SettingsManager.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand


class SkillsCommand(DirectCommand):
    """Manage skill directories: list, add, remove skill paths."""

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        config = CommandConfig(
            name="skills",
            description="Manage skill directories (list / add / remove skill paths).",
        )
        super().__init__(config)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def execute(self, context: CommandContext) -> CommandResult:
        parts = context.args.strip().split() if context.args and context.args.strip() else []

        if not parts:
            return self._show_menu()

        action = parts[0].lower()
        rest = parts[1:]

        if action == "list":
            return self._list_paths()
        elif action == "add":
            return self._add_path(rest)
        elif action == "remove":
            return self._remove_path(rest)
        else:
            return CommandResult.error_result(
                f"Unknown action '{action}'. Available: list, add <path> [--scope global|project], "
                "remove <path> [--scope global|project]"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _manager():
        from ..settings_manager import SettingsManager

        return SettingsManager()

    @staticmethod
    def _parse_scope(args: List[str]) -> Tuple[List[str], str]:
        """Extract --scope flag from *args*. Returns (remaining_args, scope).

        If --scope appears without a following value it is treated as an
        unknown flag and kept in remaining (scope stays 'global').
        """
        scope = "global"
        remaining: List[str] = []
        i = 0
        while i < len(args):
            if args[i] == "--scope":
                if i + 1 < len(args):
                    scope = args[i + 1]
                    i += 2
                else:
                    # --scope with no value: keep flag in remaining
                    remaining.append(args[i])
                    i += 1
            else:
                remaining.append(args[i])
                i += 1
        return remaining, scope

    # ------------------------------------------------------------------
    # Sub-commands
    # ------------------------------------------------------------------

    def _show_menu(self) -> CommandResult:
        panel = Panel(
            Text(
                "/skills list\n"
                "    List all active skill paths (global + project)\n\n"
                "/skills add <path> [--scope global|project]\n"
                "    Add a skill directory to settings (default scope: global)\n\n"
                "/skills remove <path> [--scope global|project]\n"
                "    Remove a skill directory from settings\n\n"
                "Global settings: ~/.deile/settings.json\n"
                "Project settings: .deile/settings.json (current directory)",
                style="dim",
            ),
            title="Skills Manager — /skills",
            border_style="cyan",
        )
        return CommandResult.success_result(panel, "rich")

    def _list_paths(self) -> CommandResult:
        mgr = self._manager()

        table = Table(title="Active Skill Paths", show_header=True, header_style="bold cyan")
        table.add_column("Scope", style="bold", width=10)
        table.add_column("Path", style="green")
        table.add_column("Exists?", width=8, justify="center")

        for scope in ("global", "project"):
            for raw in mgr.list_skills_paths(scope):
                p = Path(raw).expanduser()
                exists_str = "yes" if p.is_dir() else "no"
                table.add_row(scope, raw, exists_str)

        if table.row_count == 0:
            return CommandResult.success_result(
                Panel(
                    Text(
                        "No skill paths configured.\n"
                        "Use '/skills add <path>' to add a directory.",
                        style="dim",
                    ),
                    title="Skills",
                    border_style="cyan",
                ),
                "rich",
            )

        return CommandResult.success_result(table, "rich")

    def _add_path(self, args: List[str]) -> CommandResult:
        remaining, scope = self._parse_scope(args)

        if not remaining:
            return CommandResult.error_result(
                "Usage: /skills add <path> [--scope global|project]"
            )
        if scope not in ("global", "project"):
            return CommandResult.error_result(
                f"Invalid scope '{scope}'. Use 'global' or 'project'."
            )

        raw_path = remaining[0]
        mgr = self._manager()
        added = mgr.add_skills_path(raw_path, scope=scope)

        if added:
            msg = f"Added '{raw_path}' to {scope} skills paths."
            style = "green"
        else:
            msg = f"'{raw_path}' is already in {scope} skills paths."
            style = "yellow"

        return CommandResult.success_result(
            Panel(Text(msg, style=style), title="Skills", border_style=style),
            "rich",
        )

    def _remove_path(self, args: List[str]) -> CommandResult:
        remaining, scope = self._parse_scope(args)

        if not remaining:
            return CommandResult.error_result(
                "Usage: /skills remove <path> [--scope global|project]"
            )
        if scope not in ("global", "project"):
            return CommandResult.error_result(
                f"Invalid scope '{scope}'. Use 'global' or 'project'."
            )

        raw_path = remaining[0]
        mgr = self._manager()
        removed = mgr.remove_skills_path(raw_path, scope=scope)

        if removed:
            msg = f"Removed '{raw_path}' from {scope} skills paths."
            style = "green"
        else:
            msg = f"'{raw_path}' was not found in {scope} skills paths."
            style = "yellow"

        return CommandResult.success_result(
            Panel(Text(msg, style=style), title="Skills", border_style=style),
            "rich",
        )
