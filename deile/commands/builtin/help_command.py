"""Comando de ajuda builtin."""

import logging

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand

logger = logging.getLogger(__name__)


class HelpCommand(DirectCommand):
    """Comando /help builtin."""

    # CLI flag metadata — argparse owns --help natively, so we do NOT export
    # a cli_flag here. The CLI's custom help formatter expands argparse's
    # built-in --help with the slash command catalog (issue #126).
    cli_flag = None
    cli_help = "Show this help message and the list of available slash commands."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="help",
            description="Lista comandos disponíveis e exemplos de uso",
            action="show_help",
        )
        super().__init__(config)
        self.category = "system"

    async def execute(self, context: CommandContext) -> CommandResult:
        try:
            from ..registry import get_command_registry
            registry = get_command_registry(context.config_manager)
            args = (context.args or "").strip()

            if args:
                command = registry.get_command(args)
                if command is None:
                    return CommandResult.error_result(f"Command '/{args}' not found")

                help_content = await command.get_help()
                aliases = (
                    getattr(command, "aliases", None)
                    or getattr(getattr(command, "config", None), "aliases", None)
                    or []
                )
                aliases_info = (
                    "\n\n**Aliases:** " + ", ".join(f"/{a}" for a in aliases)
                    if aliases else ""
                )
                panel = Panel(
                    help_content + aliases_info,
                    title=f"[bold cyan]Help: /{command.name}[/bold cyan]",
                    border_style="cyan",
                )
                return CommandResult.success_result(panel, "rich")

            table = Table(title="📚 DEILE Commands (Main Names Only)", box=box.ROUNDED)
            table.add_column("Command", style="cyan", width=15)
            table.add_column("Description", style="white", width=40)
            table.add_column("Type", style="yellow", width=10)
            for command in registry.get_enabled_commands():
                cmd_type = "LLM" if command.has_prompt_template else "Direct"
                table.add_row(f"/{command.name}", command.description, cmd_type)

            footer = Text()
            footer.append("\n💡 ", style="yellow")
            footer.append("Use '/help <comando>' para ajuda específica e aliases\n", style="dim")
            footer.append("📝 ", style="blue")
            footer.append("Digite '@' para autocompletar arquivos\n", style="dim")
            footer.append("🔧 ", style="green")
            footer.append("Digite '/' para ver comandos disponíveis\n", style="dim")
            footer.append("🏷️ ", style="magenta")
            footer.append("Apenas nomes principais mostrados (aliases via /help <cmd>)", style="dim")

            help_panel = Panel(
                Group(table, footer),
                title="[bold cyan]DEILE Commands[/bold cyan]",
                border_style="cyan",
            )
            return CommandResult.success_result(
                help_panel,
                "rich",
                total_commands=len(registry.get_enabled_commands()),
            )
        except Exception as exc:
            logger.error("HelpCommand error: %s", exc)
            return CommandResult.error_result(f"Error showing help: {exc}", error=exc)
