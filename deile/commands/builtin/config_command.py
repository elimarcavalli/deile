"""Comando config builtin"""

from ..base import DirectCommand, CommandResult, CommandContext
from ..actions import CommandActions


class ConfigCommand(DirectCommand):
    """Comando /config builtin"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="config",
            description="Exibe configurações atuais do sistema (API, comandos, debug)",
            action="show_config",
            # aliases=["cfg", "settings", "conf"]
        )
        super().__init__(config)
        self.category = "system"
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Executa exibição da configuração"""
        actions = CommandActions(
            agent=context.agent,
            ui_manager=context.ui_manager,
            config_manager=context.config_manager
        )
        
        return await actions.show_config(context.args, context)