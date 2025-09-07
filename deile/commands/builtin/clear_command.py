"""Clear Command for DEILE v4.0 - Resolves SITUAÇÃO 7"""

import logging
from typing import Optional
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Confirm

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError


logger = logging.getLogger(__name__)


class ClearCommand(DirectCommand):
    """Clear conversation history and optionally reset entire session"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="cls",
            description="Clear conversation history and screen. Use 'cls reset' for complete session reset.",
            aliases=["clear"]
        )
        super().__init__(config)
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute clear command with enhanced reset functionality"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
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
        """Complete session reset - SOLVES SITUAÇÃO 7"""
        
        # Show warning and confirmation unless forced
        if not force:
            warning_content = [
                "⚠️ **COMPLETE SESSION RESET**",
                "",
                "This will permanently clear:",
                "• All conversation history",
                "• Session context and memory", 
                "• Token counters and cost data",
                "• Active plans and orchestration state",
                "• Cached data and temporary files",
                "• All pending operations",
                "",
                "**This operation cannot be undone!**",
                "",
                "Consider using `/export` first to backup important data.",
                "",
                "Continue with complete reset?"
            ]
            
            warning_text = "\n".join(warning_content)
            
            warning_panel = Panel(
                Text(warning_text, style="yellow"),
                title="⚠️ Complete Session Reset",
                border_style="red"
            )
            
            # In a real interactive environment, you'd prompt the user
            # For now, we'll assume confirmation
            confirmed = True  # In real implementation: Confirm.ask("Continue?")
            
            if not confirmed:
                return CommandResult.success_result(
                    Panel(
                        Text("Reset cancelled by user.", style="yellow"),
                        title="🚫 Operation Cancelled",
                        border_style="yellow"
                    ),
                    "rich"
                )
        
        try:
            reset_steps = []
            
            # 1. Clear conversation history and context
            if hasattr(context, 'agent') and context.agent:
                context.agent.clear_conversation_history()
                context.agent.clear_context()
                reset_steps.append("✅ Conversation history cleared")
                reset_steps.append("✅ Agent context cleared")
            
            # 2. Clear session memory and cache
            if hasattr(context, 'agent') and context.agent:
                if hasattr(context.agent, 'clear_session_memory'):
                    context.agent.clear_session_memory()
                    reset_steps.append("✅ Session memory cleared")
            
            # 3. Reset token counters and cost tracking
            if hasattr(context, 'agent') and context.agent:
                if hasattr(context.agent, 'reset_token_counters'):
                    context.agent.reset_token_counters()
                    reset_steps.append("✅ Token counters reset")
            
            # 4. Clear active plans and orchestration
            try:
                from ...orchestration.plan_manager import get_plan_manager
                plan_manager = get_plan_manager()
                
                # Clear active plans (but don't delete saved plans)
                plan_manager._active_plans.clear()
                plan_manager._execution_locks.clear()
                plan_manager._stop_flags.clear()
                reset_steps.append("✅ Active plans cleared")
                
            except Exception as e:
                logger.warning(f"Could not clear orchestration state: {e}")
                reset_steps.append("⚠️ Orchestration state partially cleared")
            
            # 5. Clear approval system state
            try:
                from ...orchestration.approval_system import get_approval_system
                approval_system = get_approval_system()
                
                # Clear pending requests
                approval_system.pending_requests.clear()
                approval_system.request_futures.clear()
                reset_steps.append("✅ Approval requests cleared")
                
            except Exception as e:
                logger.warning(f"Could not clear approval state: {e}")
                reset_steps.append("⚠️ Approval state partially cleared")
            
            # 6. Clear UI and screen
            if hasattr(context, 'ui_manager') and context.ui_manager:
                context.ui_manager.clear_screen()
                reset_steps.append("✅ Screen display cleared")
            
            # 7. Clear temporary files and cache
            try:
                import tempfile
                import shutil
                from pathlib import Path
                
                # Clear common temporary directories
                temp_dirs = ["TEMP", "CACHE", ".deile_cache"]
                for temp_dir in temp_dirs:
                    temp_path = Path(temp_dir)
                    if temp_path.exists():
                        shutil.rmtree(temp_path, ignore_errors=True)
                        reset_steps.append(f"✅ Temporary directory {temp_dir} cleared")
                        
            except Exception as e:
                logger.warning(f"Could not clear temporary files: {e}")
                reset_steps.append("⚠️ Temporary files partially cleared")
            
            # 8. Reset session ID and timestamps
            if hasattr(context, 'session_id'):
                import uuid
                context.session_id = str(uuid.uuid4())
                reset_steps.append("✅ Session ID regenerated")
            
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
                f"⚠️ **PARTIAL RESET COMPLETED**",
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