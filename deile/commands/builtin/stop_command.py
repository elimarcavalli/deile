"""Stop Command - Stop running plan execution"""

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ...orchestration.plan_manager import PlanStatus, get_plan_manager
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import split_args, truncate, wrap_command_errors


class StopCommand(DirectCommand):
    """Stop running plan execution"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="stop",
            description="Stop running plan execution.",
        )
        super().__init__(config)
        self.plan_manager = get_plan_manager()
    
    @wrap_command_errors("stop")
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute stop command"""
        parts = split_args(context)

        if not parts:
            return await self._show_stoppable_plans()

        plan_id = parts[0]
        force = len(parts) > 1 and parts[1] == "--force"

        return await self._stop_plan(plan_id, force)
    
    async def _show_stoppable_plans(self) -> CommandResult:
        """Show plans that can be stopped"""
        
        # Get running or paused plans
        running_plans = await self.plan_manager.list_plans(PlanStatus.RUNNING)
        paused_plans = await self.plan_manager.list_plans(PlanStatus.PAUSED)
        
        stoppable_plans = running_plans + paused_plans
        
        if not stoppable_plans:
            return CommandResult.success_result(
                Panel(
                    Text("No plans are currently running.\n\nUse '/plan list' to see all plans or '/run <plan_id>' to start execution.", 
                         style="yellow"),
                    title="⏹️ No Running Plans",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create table of stoppable plans
        table = Table(title=f"⏹️ Plans Available to Stop ({len(stoppable_plans)} plans)", show_header=True, header_style="bold red")
        table.add_column("Plan ID", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Status", style="yellow")
        table.add_column("Progress", style="green")
        table.add_column("Started", style="dim")
        table.add_column("Action", style="red")
        
        for plan in stoppable_plans:
            # Get detailed status
            status = await self.plan_manager.get_plan_status(plan["id"])
            
            # Status emoji
            status_emoji = "🔄" if plan["status"] == "running" else "⏸️"
            status_text = f"{status_emoji} {plan['status']}"
            
            # Progress
            if status:
                progress = status['progress']
                progress_text = f"{progress['completed']}/{progress['total']} ({progress['percentage']:.0f}%)"
            else:
                progress_text = "Unknown"
            
            # Started time
            if status and status['timing']['started_at']:
                started_at = status['timing']['started_at'][:16].replace("T", " ")
            else:
                started_at = "Unknown"
            
            # Action
            action_text = f"/stop {plan['id']}"
            
            table.add_row(
                plan["id"],
                truncate(plan["title"], 30),
                status_text,
                progress_text,
                started_at,
                action_text
            )
        
        # Add usage instructions
        usage_panel = Panel(
            Text(
                "Usage:\n"
                "• /stop <plan_id>         - Stop plan execution gracefully\n"
                "• /stop <plan_id> --force - Force stop plan immediately\n\n"
                "Stopped plans can be viewed with '/plan show <plan_id>'.\n"
                "Use '/plan delete <plan_id>' to remove stopped plans.",
                style="dim"
            ),
            title="Usage Instructions",
            border_style="dim"
        )
        
        return CommandResult.success_result(Group(table, usage_panel), "rich")
    
    async def _stop_plan(self, plan_id: str, force: bool = False) -> CommandResult:
        """Stop a specific plan"""
        
        # Validate plan exists and can be stopped
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        if plan.status not in [PlanStatus.RUNNING, PlanStatus.PAUSED]:
            raise CommandError(f"Plan '{plan_id}' is not running (status: {plan.status.value})")
        
        # Get current status before stopping
        status_before = await self.plan_manager.get_plan_status(plan_id)
        
        # Stop the plan
        success = await self.plan_manager.stop_plan(plan_id)
        if not success:
            raise CommandError(f"Failed to stop plan '{plan_id}'. Plan may not be running.")
        
        # Create result message
        stop_type = "force stopped" if force else "stopped"
        
        content_lines = [
            f"⏹️ **Plan {stop_type.title()} Successfully**",
            "",
            f"**Plan:** {plan.title}",
            f"**Plan ID:** {plan_id}",
            f"**Previous Status:** {plan.status.value}",
            "**New Status:** cancelled"
        ]
        
        # Add progress information if available
        if status_before:
            progress = status_before['progress']
            content_lines.extend([
                "",
                "**Progress at Stop:**",
                f"  • Completed Steps: {progress['completed']}/{progress['total']} ({progress['percentage']:.1f}%)",
                f"  • Failed Steps: {progress['failed']}",
                f"  • Skipped Steps: {progress['skipped']}"
            ])
            
            # Show timing info
            timing = status_before['timing']
            if timing['started_at']:
                content_lines.append(f"  • Started: {timing['started_at'][:19]}")
            
            if timing['actual_duration']:
                content_lines.append(f"  • Runtime: {timing['actual_duration']:.1f}s")
        
        # Add current steps info
        if status_before and status_before.get('current_steps'):
            current_steps = status_before['current_steps']
            content_lines.extend([
                "",
                "**Interrupted Steps:**"
            ])
            
            for step in current_steps:
                step_status = "🔄 Running" if step['status'] == 'running' else "⚠️ Waiting approval"
                content_lines.append(f"  • {step_status}: {step['description']}")
        
        # Add next steps guidance
        content_lines.extend([
            "",
            "**Next Actions:**",
            f"• Use `/plan show {plan_id}` to see detailed stop information",
            f"• Use `/plan delete {plan_id}` to remove this plan",
            "• Create a new plan with `/plan create <objective>` if needed"
        ])
        
        # Special notes for force stop
        if force:
            content_lines.extend([
                "",
                "⚠️ **Force Stop Note:**",
                "The plan was force stopped immediately.",
                "Some cleanup operations may not have completed."
            ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="red"),
            title="⏹️ Plan Stopped",
            border_style="red",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Stop running plan execution

Usage:
  /stop                    Show plans that can be stopped
  /stop <plan_id>          Stop plan execution gracefully
  /stop <plan_id> --force  Force stop plan immediately

Stop Behavior:
  • Graceful stop: Allows current step to complete, then stops
  • Force stop: Immediately terminates execution
  • Stopped plans are marked as 'cancelled'
  • Progress is preserved and can be reviewed

Examples:
  /stop                    List all running plans
  /stop abc123             Stop plan abc123 gracefully
  /stop abc123 --force     Force stop plan abc123 immediately

Plan States:
  • Only running or paused plans can be stopped
  • Stopped plans cannot be resumed
  • Use /plan delete to remove stopped plans
  • Create new plans with /plan create

Related Commands:
  • /run <plan_id> - Start plan execution
  • /plan show <plan_id> - View plan details
  • /plan list - Show all plans
  • /plan delete <plan_id> - Remove plan"""