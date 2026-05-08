"""Comando debug builtin"""

from ..actions import CommandActions
from ..base import CommandContext, CommandResult, DirectCommand


class DebugCommand(DirectCommand):
    """Comando /debug builtin"""

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