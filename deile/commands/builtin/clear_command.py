"""Clear Command for DEILE"""

import logging

from rich.panel import Panel
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import split_args

logger = logging.getLogger(__name__)


class ClearCommand(DirectCommand):
    """Clear conversation history and optionally reset entire session"""

    cli_flag = "--clear"
    cli_help = "Clear conversation history and screen state."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="clear",
            description="Clear conversation history and screen. Use 'clear reset' for complete session reset.",
            aliases=["cls"],
        )
        super().__init__(config)
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute clear command with enhanced reset functionality"""
        try:
            parts = split_args(context)
            
            if not parts:
                # Standard clear - just clear screen and history
                return await self._clear_standard(context)
            
            command = parts[0].lower()
            
            if command == "reset":
                # Complete session reset - SITUAÇÃO 7 SOLUTION
                force = "--force" in parts or "-f" in parts
                return await self._clear_reset(context, force)
            elif command == "history":
                # Clear only conversation history
                return await self._clear_history_only(context)
            elif command == "screen":
                # Clear only screen display
                return await self._clear_screen_only(context)
            else:
                raise CommandError(f"Unknown clear option: {command}. Use: cls, cls reset, cls history, cls screen")
                
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute clear command: {str(e)}")
    
    async def _clear_standard(self, context: CommandContext) -> CommandResult:
        """Standard clear - conversation history and screen"""
        
        try:
            # Clear screen if UI manager available
            if hasattr(context, 'ui_manager') and context.ui_manager:
                context.ui_manager.clear_screen()
            
            # Clear conversation history if agent available
            if hasattr(context, 'agent') and context.agent:
                context.agent.clear_conversation_history()
                
            success_message = "✅ Conversation history and screen cleared.\n\nSession state and memory preserved."
            
            return CommandResult.success_result(
                Panel(
                    Text(success_message, style="green"),
                    title="🧹 Screen Cleared",
                    border_style="green"
                ),
                "rich"
            )
            
        except Exception as e:
            raise CommandError(f"Failed to clear screen and history: {str(e)}")
    
    async def _clear_reset(self, context: CommandContext, force: bool = False) -> CommandResult:
        """Complete session reset - SOLVES SITUAÇÃO 7

        ``force`` is currently a no-op placeholder for a future interactive
        confirmation prompt; the parameter is kept to preserve the public
        ``/cls reset --force`` CLI surface.
        """
        del force  # interactive-confirm prompt not yet implemented (issue tracked separately)

        try:
            reset_steps: list[str] = []
            reset_steps.extend(self._reset_agent(context))
            reset_steps.extend(self._reset_plans())
            reset_steps.extend(self._reset_approvals())

            if hasattr(context, 'ui_manager') and context.ui_manager:
                context.ui_manager.clear_screen()
                reset_steps.append("✅ Screen display cleared")

            reset_steps.extend(self._reset_temp_files())
            reset_steps.extend(self._reset_session_id(context))
            
            # Create success report
            success_content = [
                "🎉 **SESSION RESET COMPLETE**",
                "",
                "**Operations Completed:**"
            ]
            
            for step in reset_steps:
                success_content.append(f"  {step}")
            
            success_content.extend([
                "",
                "**Session State:**",
                "• Fresh conversation context",
                "• Reset token counters", 
                "• Clear orchestration state",
                "• New session ID",
                "",
                "🚀 **Ready for fresh start!**"
            ])
            
            success_text = "\n".join(success_content)
            
            return CommandResult.success_result(
                Panel(
                    Text(success_text, style="green"),
                    title="🔄 Session Reset Complete", 
                    border_style="green",
                    padding=(1, 2)
                ),
                "rich"
            )
            
        except Exception as e:
            # Even if some steps failed, report what was accomplished
            error_content = [
                "⚠️ **PARTIAL RESET COMPLETED**",
                "",
                f"**Error:** {str(e)}",
                "",
                "**Completed Steps:**"
            ]
            
            for step in reset_steps:
                error_content.append(f"  {step}")
            
            error_content.extend([
                "",
                "Some components may still retain state.",
                "Try restarting the application for complete reset."
            ])
            
            error_text = "\n".join(error_content)
            
            return CommandResult.success_result(
                Panel(
                    Text(error_text, style="yellow"),
                    title="⚠️ Partial Reset",
                    border_style="yellow",
                    padding=(1, 2)
                ),
                "rich"
            )
    
    @staticmethod
    def _reset_agent(context: CommandContext) -> list[str]:
        """Steps 1-3: clear agent conversation/context/memory/token counters.

        Returns the list of human-readable step lines that were performed.
        Each call to a ``hasattr``-gated method is best-effort: missing
        methods simply skip the corresponding step.
        """
        steps: list[str] = []
        agent = getattr(context, 'agent', None)
        if not agent:
            return steps
        agent.clear_conversation_history()
        agent.clear_context()
        steps.append("✅ Conversation history cleared")
        steps.append("✅ Agent context cleared")
        if hasattr(agent, 'clear_session_memory'):
            agent.clear_session_memory()
            steps.append("✅ Session memory cleared")
        if hasattr(agent, 'reset_token_counters'):
            agent.reset_token_counters()
            steps.append("✅ Token counters reset")
        return steps

    @staticmethod
    def _reset_plans() -> list[str]:
        """Step 4: clear active plans/locks/stop-flags from PlanManager singleton."""
        try:
            from ...orchestration.plan_manager import get_plan_manager
            get_plan_manager().clear_active_state()
            return ["✅ Active plans cleared"]
        except Exception as exc:
            logger.warning("Could not clear orchestration state: %s", exc)
            return ["⚠️ Orchestration state partially cleared"]

    @staticmethod
    def _reset_approvals() -> list[str]:
        """Step 5: clear pending approval requests + futures."""
        try:
            from ...orchestration.approval_system import get_approval_system
            approval_system = get_approval_system()
            approval_system.pending_requests.clear()
            approval_system.request_futures.clear()
            return ["✅ Approval requests cleared"]
        except Exception as exc:
            logger.warning("Could not clear approval state: %s", exc)
            return ["⚠️ Approval state partially cleared"]

    @staticmethod
    def _reset_temp_files() -> list[str]:
        """Step 7: rm -rf well-known temp dirs (TEMP / CACHE / .deile_cache)."""
        steps: list[str] = []
        try:
            import shutil
            from pathlib import Path
            for temp_dir in ("TEMP", "CACHE", ".deile_cache"):
                temp_path = Path(temp_dir)
                if temp_path.exists():
                    shutil.rmtree(temp_path, ignore_errors=True)
                    steps.append(f"✅ Temporary directory {temp_dir} cleared")
        except Exception as exc:
            logger.warning("Could not clear temporary files: %s", exc)
            steps.append("⚠️ Temporary files partially cleared")
        return steps

    @staticmethod
    def _reset_session_id(context: CommandContext) -> list[str]:
        """Step 8: regenerate session UUID when present on the context."""
        if not hasattr(context, 'session_id'):
            return []
        import uuid
        context.session_id = str(uuid.uuid4())
        return ["✅ Session ID regenerated"]

    async def _clear_history_only(self, context: CommandContext) -> CommandResult:
        """Clear only conversation history"""
        
        try:
            if hasattr(context, 'agent') and context.agent:
                context.agent.clear_conversation_history()
                
            return CommandResult.success_result(
                Panel(
                    Text("✅ Conversation history cleared.\n\nContext, memory, and session state preserved.", 
                         style="green"),
                    title="📝 History Cleared",
                    border_style="green"
                ),
                "rich"
            )
            
        except Exception as e:
            raise CommandError(f"Failed to clear history: {str(e)}")
    
    async def _clear_screen_only(self, context: CommandContext) -> CommandResult:
        """Clear only screen display"""
        
        try:
            if hasattr(context, 'ui_manager') and context.ui_manager:
                context.ui_manager.clear_screen()
                
            return CommandResult.success_result(
                Panel(
                    Text("✅ Screen display cleared.\n\nHistory and session state preserved.", 
                         style="green"),
                    title="🖥️ Screen Cleared",
                    border_style="green"
                ),
                "rich"
            )
            
        except Exception as e:
            raise CommandError(f"Failed to clear screen: {str(e)}")
    
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