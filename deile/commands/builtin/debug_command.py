"""Comando debug builtin"""

from ..base import DirectCommand, CommandResult, CommandContext
from ..actions import CommandActions


class DebugCommand(DirectCommand):
    """Comando /debug builtin"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="debug",
            description="Toggle do modo debug (logs detalhados + request/response files)",
            action="toggle_debug_mode",
            # aliases=["dbg", "verbose", "log"]
        )
        super().__init__(config)
        self.category = "system"
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Executa toggle de debug"""
        actions = CommandActions(
            agent=context.agent,
            ui_manager=context.ui_manager,
            config_manager=context.config_manager
        )
        
        return await actions.toggle_debug_mode(context.args, context)