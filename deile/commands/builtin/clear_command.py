"""Comando clear builtin"""

from ..base import DirectCommand, CommandResult, CommandContext
from ..actions import CommandActions


class ClearCommand(DirectCommand):
    """Comando /clear builtin"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="cls",
            description="Limpa histórico e tela. Use 'cls reset' para reset completo da sessão",
            action="clear_session",
            aliases=["clear"]
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
    
    def get_help(self) -> str:
        """Get detailed help for clear command"""
        return """Clear conversation history and screen

Usage:
  /cls                  Clear conversation history and screen  
  /cls reset            Complete session reset (recommended)

Clear Options:
  • /cls         - Clear conversation history and screen only
  • /cls reset   - Complete session reset including:
                   • Conversation history
                   • Context data and memory
                   • Token counters and costs
                   • Active plans and orchestration
                   • Audit logs buffer
                   • Screen display

When to Use:
  • /cls         - Quick cleanup for continuing work
  • /cls reset   - Fresh start, troubleshooting, or context overflow

Examples:
  /cls                  Quick clear
  /cls reset            Complete fresh start

Effects:
  • Clear: Maintains session state, clears history/screen
  • Reset: Complete fresh session, all data cleared

Related Commands:
  • /context - View current context before clearing
  • /export - Backup data before reset
  • /status - Check session state

Note: Reset operation cannot be undone. Use /export first if you need to preserve any data."""