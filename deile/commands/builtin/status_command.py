"""Comando status builtin"""

from ..base import DirectCommand, CommandResult, CommandContext
from ..actions import CommandActions


class StatusCommand(DirectCommand):
    """Comando /status builtin"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="status",
            description="Mostra informações sobre o status do sistema e agente",
            action="show_system_status",
            # aliases=["info", "stat", "sys"]
        )
        super().__init__(config)
        self.category = "system"
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Executa exibição do status"""
        actions = CommandActions(
            agent=context.agent,
            ui_manager=context.ui_manager,
            config_manager=context.config_manager
        )
        
        return await actions.show_system_status(context.args, context)