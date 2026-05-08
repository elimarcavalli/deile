"""Version Command — print DEILE version (issue #126).

Maps to both:
  • the ``/version`` slash command in interactive mode, and
  • the ``--version`` CLI flag (auto-generated from cli_flag metadata).
"""

from __future__ import annotations

import logging

from rich.panel import Panel
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand

logger = logging.getLogger(__name__)


class VersionCommand(DirectCommand):
    """``/version`` — print DEILE version and metadata."""

    cli_flag = "--version"
    cli_help = "Show DEILE version and exit."
    cli_requires_provider = False

    def __init__(self) -> None:
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="version",
            description="Show DEILE version, build metadata and feature flags.",
            aliases=["ver"],
        )
        super().__init__(config)
        self.category = "system"

    async def execute(self, context: CommandContext) -> CommandResult:
        """Render version information."""
        try:
            from deile.__version__ import (FEATURES, __build_date__,
                                           __build_number__, __description__,
                                           __license__, __title__, __version__)
        except ImportError as exc:  # pragma: no cover — package always present
            logger.error("Failed to import deile version: %s", exc)
            return CommandResult.error_result(
                f"Could not determine DEILE version: {exc}",
                error=exc,
            )

        feature_lines = [f"  • {k}" for k, v in FEATURES.items() if v]
        body = (
            f"[bold cyan]{__title__}[/bold cyan] [bold]v{__version__}[/bold]\n"
            f"{__description__}\n\n"
            f"[dim]Build:[/dim] {__build_number__}  "
            f"[dim]Date:[/dim] {__build_date__}  "
            f"[dim]License:[/dim] {__license__}\n\n"
            f"[bold]Active features:[/bold]\n"
            + "\n".join(feature_lines)
        )

        panel = Panel(
            Text.from_markup(body),
            title="[bold cyan]DEILE Version[/bold cyan]",
            border_style="cyan",
        )
        return CommandResult.success_result(
            panel,
            "rich",
            version=__version__,
            build_date=__build_date__,
            build_number=__build_number__,
        )
