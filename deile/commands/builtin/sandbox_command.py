"""Sandbox status command — informational only.

The /sandbox toggle does not provide isolation. Tools always run on the host
with the DEILE process's full privileges. The historical Docker wiring was
removed (issue #55) because the manager class was never plugged into any
execution tool. See also issues #54 (PluginSandbox skeleton) and #57 (the
bash_execute `sandbox=True` flag controls PTY only).
"""

from __future__ import annotations

import logging

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...config.manager import CommandConfig
from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import split_args

logger = logging.getLogger(__name__)


class SandboxCommand(DirectCommand):
    """Sandbox status and toggle (informational only — no real isolation)."""

    def __init__(self):
        super().__init__(
            CommandConfig(
                name="sandbox",
                description="Sandbox status and toggle (informational only)",
            )
        )
        self.sandbox_enabled = False

    async def execute(self, context: CommandContext) -> CommandResult:
        parts: list[str] = split_args(context)

        if not parts:
            return await self._show_sandbox_status()

        action = parts[0].lower()
        if action in ("on", "enable", "true"):
            return await self._toggle_sandbox(True)
        if action in ("off", "disable", "false"):
            return await self._toggle_sandbox(False)
        if action in ("status", "info"):
            return await self._show_sandbox_status()
        if action in ("config", "configure"):
            return await self._show_sandbox_config()
        raise CommandError(f"Unknown sandbox action: {action}")

    async def _show_sandbox_status(self) -> CommandResult:
        status_emoji = "🟢" if self.sandbox_enabled else "🔴"
        status_text = "ENABLED" if self.sandbox_enabled else "DISABLED"
        status_color = "green" if self.sandbox_enabled else "red"

        status_table = Table(title=f"{status_emoji} Sandbox Status", show_header=False)
        status_table.add_column("Property", style="bold cyan")
        status_table.add_column("Value", style=status_color)
        status_table.add_column("Description", style="dim")

        status_table.add_row(
            "Mode", f"{status_emoji} {status_text}", "Toggle state (informational only)"
        )
        status_table.add_row("Isolation", "None", "No real isolation in either mode")
        status_table.add_row(
            "File Access", "Unrestricted", "Filesystem permissions (host-level)"
        )
        status_table.add_row("Network", "Open", "Network access (host-level)")
        status_table.add_row(
            "System Calls", "Direct", "System interaction (host-level)"
        )

        info_text = (
            "ℹ️ **Sandbox toggle is informational only.**\n\n"
            "Tools always run on the host with the DEILE process's full privileges.\n"
            "The bash_execute `sandbox=True` parameter only disables PTY allocation;\n"
            "it does NOT containerize or isolate the command.\n\n"
            "🚨 **Do not rely on this state for security guarantees.**\n"
            "See issues #54 (PluginSandbox), #55 (Docker wiring removed), #57 (PTY-only)."
        )
        info_panel = Panel(
            Text(info_text, style="yellow"),
            title="🛡️ Security Status",
            border_style="yellow",
        )

        actions_text = (
            "🚀 **Quick Actions**\n\n"
            f"/sandbox {'off' if self.sandbox_enabled else 'on'}     - Toggle the informational flag\n"
            "/sandbox config   - Show what is (and is not) configurable\n"
            "/permissions      - Manage real permission rules\n"
            "/tools            - List tools and their security level"
        )
        actions_panel = Panel(
            Text(actions_text, style="blue"),
            title="🎛️ Controls",
            border_style="blue",
        )

        return CommandResult.success_result(
            Group(status_table, "", info_panel, "", actions_panel),
            "rich",
        )

    async def _toggle_sandbox(self, enabled: bool) -> CommandResult:
        old_status = self.sandbox_enabled
        self.sandbox_enabled = enabled
        action_text = "enabled" if enabled else "disabled"
        emoji = "🟢" if enabled else "🔴"
        color = "green" if enabled else "red"

        if old_status == enabled:
            return CommandResult.success_result(
                Panel(
                    Text(f"Sandbox is already {action_text}.", style=color),
                    title=f"{emoji} No Change",
                    border_style=color,
                ),
                "rich",
            )

        text = (
            f"⚠️ **Sandbox toggle is now {action_text}.**\n\n"
            "Reminder: this toggle is informational only. Tools continue to run\n"
            "on the host regardless of the toggle state. Use `/permissions` for\n"
            "real access control."
        )

        return CommandResult.success_result(
            Panel(
                Text(text, style=color),
                title=f"{emoji} Sandbox {action_text.title()}",
                border_style=color,
                padding=(1, 2),
            ),
            "rich",
        )

    async def _show_sandbox_config(self) -> CommandResult:
        config_table = Table(
            title="⚙️ Sandbox Configuration",
            show_header=True,
            header_style="bold yellow",
        )
        config_table.add_column("Setting", style="cyan")
        config_table.add_column("Value", style="white")
        config_table.add_column("Description", style="dim")

        config_table.add_row(
            "Execution Mode", "Host (no isolation)", "Tools run with DEILE's privileges"
        )
        config_table.add_row(
            "File System", "Unrestricted", "Access controlled by `/permissions`"
        )
        config_table.add_row(
            "Network Policy", "Unrestricted", "Network rules controlled elsewhere"
        )
        config_table.add_row(
            "Resource Limits", "None", "No CPU/memory/disk limits enforced here"
        )
        config_table.add_row(
            "Monitoring", "Audit log", "Every tool execution is recorded"
        )

        notes_text = (
            "ℹ️ **What is and is not configurable here**\n\n"
            "The `/sandbox` command exposes only an informational toggle. There is no\n"
            "configuration file, no environment variable, and no programmatic policy\n"
            "knob that turns this command into real isolation.\n\n"
            "**Real access control lives elsewhere:**\n"
            "  • `/permissions` — permission rules consulted by tools at runtime\n"
            "  • `bash_execute(security_level=…)` — per-call risk level for shell commands\n"
            "  • Audit log — every tool execution is recorded\n\n"
            "**If you want real containerization**, file an issue describing the threat\n"
            "model and acceptance criteria — Docker wiring was previously removed\n"
            "(issue #55) because it was promised but never plugged in."
        )
        notes_panel = Panel(
            Text(notes_text, style="blue"),
            title="📋 Notes",
            border_style="blue",
        )

        return CommandResult.success_result(
            Group(config_table, "", notes_panel),
            "rich",
        )

    def get_help(self) -> str:
        return """Sandbox status and informational toggle.

The /sandbox toggle does NOT provide isolation. Tools always run on the host
with the DEILE process's full privileges. See issues #54/#55/#57 for context.

Usage:
  /sandbox              Show current sandbox status
  /sandbox on           Set the informational flag to ON
  /sandbox off          Set the informational flag to OFF
  /sandbox status       Same as /sandbox (alias)
  /sandbox config       Show what is (and is not) configurable

Real access control:
  • /permissions                 Manage permission rules consulted at runtime
  • bash_execute(security_level) Per-call risk level for shell commands
  • Audit log                    Every tool execution is recorded

Related:
  • /tools                       List tools and their declared security level
  • /permissions check           Test whether an action would be allowed
"""
