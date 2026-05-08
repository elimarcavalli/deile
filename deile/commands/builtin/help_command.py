"""Comando de ajuda builtin"""

from ..actions import CommandActions
from ..base import CommandContext, CommandResult, DirectCommand


class HelpCommand(DirectCommand):
    """Comando /help builtin"""

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
        """Executa comando de ajuda"""
        actions = CommandActions(
            agent=context.agent,
            ui_manager=context.ui_manager,
            config_manager=context.config_manager
        )
        
        return await actions.show_help(context.args, context)