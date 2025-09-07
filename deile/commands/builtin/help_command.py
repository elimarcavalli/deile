"""Comando de ajuda builtin"""

from ..base import DirectCommand, CommandResult, CommandContext
from ..actions import CommandActions


class HelpCommand(DirectCommand):
    """Comando /help builtin"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="?",
            description="Lista comandos disponíveis e exemplos de uso",
            action="show_help",
            # aliases=["h", "?"]
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