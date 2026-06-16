"""Skills management command (/skills) — issue #104.

Provides a text-based menu to list, add and remove skill directories
from the global (~/.deile/settings.json) and project (.deile/settings.json)
settings layers managed by SettingsManager.
"""

from __future__ import annotations

from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import get_agent, split_args


class SkillsCommand(DirectCommand):
    """Manage skill directories: list, add, remove skill paths."""

    cli_flag = "--skills"
    cli_help = "List configured skill directories and exit."
    cli_requires_provider = False

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
        parts = split_args(context)

        if not parts:
            return self._show_menu()

        action = parts[0].lower()
        rest = parts[1:]

        if action == "list":
            return self._list_paths()
        elif action == "add":
            return self._add_path(rest, context)
        elif action == "remove":
            return self._remove_path(rest, context)
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
    def _parse_scope(args: list[str]) -> tuple[list[str], str]:
        """Extract --scope flag from *args*. Returns (remaining_args, scope).

        If --scope appears without a following value it is treated as an
        unknown flag and kept in remaining (scope stays 'global').
        """
        scope = "global"
        remaining: list[str] = []
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
        body = Text()
        body.append("  /skills list\n", style="bold bright_cyan")
        body.append(
            "      List all active skill paths (global + project)\n\n", style="white"
        )
        body.append("  /skills add ", style="bold bright_cyan")
        body.append("<path>", style="bold yellow")
        body.append(" [--scope global|project]\n", style="dim cyan")
        body.append(
            "      Add a skill directory to settings (default scope: global)\n\n",
            style="white",
        )
        body.append("  /skills remove ", style="bold bright_cyan")
        body.append("<path>", style="bold yellow")
        body.append(" [--scope global|project]\n", style="dim cyan")
        body.append("      Remove a skill directory from settings\n\n", style="white")
        body.append("  Global:  ", style="dim")
        body.append("~/.deile/settings.json\n", style="bright_blue")
        body.append("  Project: ", style="dim")
        body.append(".deile/settings.json", style="bright_blue")
        body.append("  (current directory)", style="dim")

        panel = Panel(
            body,
            title="[bold cyan] Skills Manager [/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
        return CommandResult.success_result(panel, "rich")

    def _list_paths(self) -> CommandResult:
        mgr = self._manager()

        table = Table(
            title="[bold cyan]Active Skill Paths[/bold cyan]",
            show_header=True,
            header_style="bold bright_cyan",
            border_style="cyan",
            row_styles=["", "dim"],
        )
        table.add_column("Scope", style="bold magenta")
        table.add_column("Path", style="bright_green")
        table.add_column("Exists?", justify="center")

        for scope in ("global", "project"):
            for raw in mgr.list_skills_paths(scope):
                p = Path(raw).expanduser()
                exists_str = "[green]yes[/green]" if p.is_dir() else "[red]no[/red]"
                table.add_row(scope, raw, exists_str)

        if table.row_count == 0:
            body = Text()
            body.append("No skill paths configured.\n", style="yellow")
            body.append("Use ", style="dim")
            body.append("/skills add <path>", style="bold bright_cyan")
            body.append(" to add a directory.", style="dim")
            return CommandResult.success_result(
                Panel(
                    body,
                    title="[bold cyan] Skills [/bold cyan]",
                    border_style="cyan",
                    padding=(1, 2),
                ),
                "rich",
            )

        return CommandResult.success_result(table, "rich")

    @staticmethod
    def _hot_reload(context: "CommandContext") -> str:
        """Trigger skills reload on the running agent. Returns a status suffix."""
        agent = get_agent(context)
        if agent is None:
            return ""
        reload_fn = getattr(agent, "reload_skills", None)
        if reload_fn is None:
            return ""
        count = reload_fn()
        return f" {count} skill(s) now active."

    def _add_path(self, args: list[str], context: "CommandContext") -> CommandResult:
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
        # P2-3: detailed return tells apart "added" / "already_present" /
        # "denied" / "io_error" so the rendered panel doesn't lie about
        # success when a permission denial silently no-op'd the call.
        added, reason = mgr.add_skills_path_detailed(raw_path, scope=scope)

        # SettingsManager resolves the path to absolute; show what was stored.
        stored = mgr.list_skills_paths(scope)[-1] if added else raw_path

        body = Text()
        if added:
            suffix = self._hot_reload(context)
            body.append(f"Added to {scope} skills paths.\n", style="bright_green")
            body.append(stored, style="bright_blue")
            if suffix:
                body.append(f"\n{suffix}", style="dim")
            style = "green"
        elif reason == "denied":
            body.append(
                "Permission denied — settings writes are fail-closed by "
                "default (issue #125).\n",
                style="bright_red",
            )
            body.append(
                "Add a 'settings_write_interactive' rule to "
                "config/permissions.yaml to enable. See "
                "docs/system_design/09-CONFIGURACAO.md for the snippet.\n",
                style="dim",
            )
            body.append(raw_path, style="dim")
            style = "red"
        elif reason == "io_error":
            body.append("Write failed — see logs for details.\n", style="bright_red")
            body.append(raw_path, style="dim")
            style = "red"
        else:  # already_present
            body.append("Already present — no change.\n", style="yellow")
            body.append(stored, style="bright_blue")
            style = "yellow"

        return CommandResult.success_result(
            Panel(
                body,
                title="[bold cyan] Skills — add [/bold cyan]",
                border_style=style,
                padding=(1, 2),
            ),
            "rich",
        )

    def _remove_path(self, args: list[str], context: "CommandContext") -> CommandResult:
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
        # P2-3: detailed return — see _add_path.
        removed, reason = mgr.remove_skills_path_detailed(raw_path, scope=scope)

        body = Text()
        if removed:
            suffix = self._hot_reload(context)
            body.append(f"Removed from {scope} skills paths.\n", style="bright_green")
            body.append(raw_path, style="dim")
            if suffix:
                body.append(f"\n{suffix}", style="dim")
            style = "green"
        elif reason == "denied":
            body.append(
                "Permission denied — settings writes are fail-closed by "
                "default (issue #125).\n",
                style="bright_red",
            )
            body.append(raw_path, style="dim")
            style = "red"
        elif reason == "io_error":
            body.append("Write failed — see logs for details.\n", style="bright_red")
            body.append(raw_path, style="dim")
            style = "red"
        else:  # not_found
            body.append("Not found — no change.\n", style="yellow")
            body.append(raw_path, style="dim")
            style = "yellow"

        return CommandResult.success_result(
            Panel(
                body,
                title="[bold cyan] Skills — remove [/bold cyan]",
                border_style=style,
                padding=(1, 2),
            ),
            "rich",
        )
