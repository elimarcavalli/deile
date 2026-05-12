"""Comando debug builtin."""

import logging
from pathlib import Path

from rich.panel import Panel
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand

logger = logging.getLogger(__name__)


class DebugCommand(DirectCommand):
    """Comando /debug builtin — toggle do modo debug."""

    # --debug is a *modifier* flag, not a one-shot dispatcher. It toggles
    # ``Settings.debug_enabled`` at startup and then yields control back to
    # the normal flow (interactive REPL or one-shot message). The CLI honors
    # this by reading ``cli_dispatch=False`` — no special-casing in cli.py.
    cli_flag = "--debug"
    cli_help = "Enable debug mode (verbose logs + request/response dumps)."
    cli_requires_provider = False
    cli_dispatch = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="debug",
            description="Toggle do modo debug (logs detalhados + request/response files)",
            action="toggle_debug_mode",
        )
        super().__init__(config)
        self.category = "system"

    async def execute(self, context: CommandContext) -> CommandResult:
        if context.config_manager is None:
            return CommandResult.error_result("Configuration manager not available")

        try:
            current_config = context.config_manager.get_config()
            current_debug = current_config.system.debug_mode
            new_debug_state = not current_debug
            context.config_manager.update_debug_mode(new_debug_state)

            if new_debug_state:
                logging.getLogger().setLevel(logging.DEBUG)
                Path("logs/debug").mkdir(parents=True, exist_ok=True)
                panel = Panel(
                    Text.from_markup(
                        "[green]✅ Debug Mode ATIVADO[/green]\n\n"
                        "📝 Logs detalhados: [cyan]logs/deile.log[/cyan]\n"
                        "📥 Request logs: [cyan]logs/debug/request_*.json[/cyan]\n"
                        "📤 Response logs: [cyan]logs/debug/response_*.json[/cyan]\n"
                        "🔍 Debug info: [cyan]logs/debug/debug_*.json[/cyan]\n\n"
                        "[dim]Use '/debug' novamente para desativar[/dim]"
                    ),
                    title="🐛 Debug System",
                    border_style="green",
                )
            else:
                logging.getLogger().setLevel(logging.INFO)
                panel = Panel(
                    Text.from_markup(
                        "[yellow]⚠️ Debug Mode DESATIVADO[/yellow]\n\n"
                        "📝 Apenas logs essenciais serão mantidos\n"
                        "🗑️ Logs de request/response pausados\n\n"
                        "[dim]Use '/debug' novamente para reativar[/dim]"
                    ),
                    title="🐛 Debug System",
                    border_style="yellow",
                )

            return CommandResult.success_result(
                panel,
                "rich",
                debug_mode=new_debug_state,
                previous_state=current_debug,
            )
        except Exception as exc:
            logger.error("DebugCommand error: %s", exc)
            return CommandResult.error_result(f"Error toggling debug: {exc}", error=exc)
