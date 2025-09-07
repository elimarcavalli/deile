"""Comando clear builtin"""

from ..base import DirectCommand, CommandResult, CommandContext
from ..actions import CommandActions


class ClearCommand(DirectCommand):
    """Comando /clear builtin"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="cls",
            description="Limpa o histórico da sessão e a tela do console",
            action="clear_session",
            # aliases=["cls", "limpar", "reset"]
        )
        super().__init__(config)
        self.category = "system"
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Executa limpeza da sessão"""
        actions = CommandActions(
            agent=context.agent,
            ui_manager=context.ui_manager,
            config_manager=context.config_manager
        )
        
        return await actions.clear_session(context.args, context)