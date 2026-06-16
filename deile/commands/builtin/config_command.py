"""Comando config builtin."""

import logging

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from ..base import CommandContext, CommandResult, DirectCommand

logger = logging.getLogger(__name__)


class ConfigCommand(DirectCommand):
    """Comando /config builtin — exibe configurações atuais."""

    cli_flag = "--config"
    cli_help = "Show current DEILE configuration and exit."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig

        config = CommandConfig(
            name="config",
            description="Exibe configurações atuais do sistema (API, comandos, debug)",
            action="show_config",
        )
        super().__init__(config)
        self.category = "system"

    async def execute(self, context: CommandContext) -> CommandResult:
        if context.config_manager is None:
            return CommandResult.error_result("Configuration manager not available")

        try:
            config = context.config_manager.get_config()
        except Exception as exc:
            logger.error("ConfigCommand: get_config failed: %s", exc)
            return CommandResult.error_result(f"Error showing config: {exc}", error=exc)

        system_table = Table(title="🔧 System Configuration", box=box.ROUNDED)
        system_table.add_column("Setting", style="cyan")
        system_table.add_column("Value", style="green")
        system_table.add_row(
            "Debug Mode", "✅ Enabled" if config.system.debug_mode else "❌ Disabled"
        )
        system_table.add_row("Log Level", config.system.log_level)
        system_table.add_row(
            "Log Requests", "✅ Yes" if config.system.log_requests else "❌ No"
        )
        system_table.add_row(
            "Log Responses", "✅ Yes" if config.system.log_responses else "❌ No"
        )

        gemini_table = Table(title="🤖 Gemini Configuration", box=box.ROUNDED)
        gemini_table.add_column("Parameter", style="cyan")
        gemini_table.add_column("Value", style="green")
        gemini_table.add_row("Model", config.gemini.model_name)
        gemini_table.add_row(
            "Temperature",
            str(config.gemini.generation_config.get("temperature", "N/A")),
        )
        gemini_table.add_row(
            "Max Output Tokens",
            str(config.gemini.generation_config.get("max_output_tokens", "N/A")),
        )
        gemini_table.add_row(
            "Top K", str(config.gemini.generation_config.get("top_k", "N/A"))
        )
        gemini_table.add_row(
            "Function Calling",
            config.gemini.tool_config.get("function_calling_config", {}).get(
                "mode", "N/A"
            ),
        )

        commands_table = Table(title="⚡ Commands Status", box=box.ROUNDED)
        commands_table.add_column("Command", style="cyan")
        commands_table.add_column("Status", style="green")
        commands_table.add_column("Type", style="yellow")
        for cmd_name, cmd_config in config.commands.items():
            status = "✅ Enabled" if cmd_config.enabled else "❌ Disabled"
            cmd_type = "LLM" if cmd_config.prompt_template else "Direct"
            commands_table.add_row(f"/{cmd_name}", status, cmd_type)

        config_panel = Panel(
            Group(system_table, gemini_table, commands_table),
            title="[bold cyan]DEILE Configuration[/bold cyan]",
            border_style="cyan",
        )
        return CommandResult.success_result(
            config_panel,
            "rich",
            config_sections=["system", "gemini", "commands"],
        )
