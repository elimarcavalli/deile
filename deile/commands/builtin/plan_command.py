"""Plan Command - Create and manage execution plans"""

from typing import Dict, Any, Optional
import asyncio
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...orchestration.plan_manager import get_plan_manager, PlanStatus


class PlanCommand(DirectCommand):
    """Create and manage autonomous execution plans"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="plan",
            description="Create and manage autonomous execution plans.",
            aliases=["p"]
        )
        super().__init__(config)
        self.plan_manager = get_plan_manager()
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute plan command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # List existing plans
                return await self._list_plans()
            
            command = parts[0]
            
            if command == "create":
                # Create new plan
                if len(parts) < 2:
                    raise CommandError("create command requires objective: /plan create <objective>")
                objective = " ".join(parts[1:])
                return await self._create_plan(objective, context)
            
            elif command == "show" or command == "status":
                # Show plan details
                if len(parts) < 2:
                    raise CommandError("show command requires plan ID: /plan show <plan_id>")
                plan_id = parts[1]
                return await self._show_plan(plan_id)
            
            elif command == "list":
                # Explicit list command
                status_filter = None
                if len(parts) > 1:
                    try:
                        status_filter = PlanStatus(parts[1])
                    except ValueError:
                        raise CommandError(f"Invalid status filter: {parts[1]}")
                return await self._list_plans(status_filter)
            
            elif command == "delete":
                # Delete plan
                if len(parts) < 2:
                    raise CommandError("delete command requires plan ID: /plan delete <plan_id>")
                plan_id = parts[1]
                return await self._delete_plan(plan_id)
            
            else:
                # Assume it's an objective for creating a plan
                objective = " ".join(parts)
                return await self._create_plan(objective, context)
            
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute plan command: {str(e)}")
    
    async def _create_plan(self, objective: str, context: CommandContext) -> CommandResult:
        """Create a new execution plan"""
        
        # Extract context information
        plan_context = {
            "working_directory": getattr(context, 'working_directory', '.'),
            "session_id": getattr(context, 'session_id', 'default'),
            "user_input": objective
        }
        
        # Show progress while creating
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            progress.add_task(description="Creating execution plan...", total=None)
            
            # Create plan
            plan = await self.plan_manager.create_plan(
                title=f"Plan for: {objective[:50]}{'...' if len(objective) > 50 else ''}",
                description=objective,
                objective=objective,
                context=plan_context
            )
        
        # Create success panel
        content_lines = [
            f"✅ **Plan Created Successfully**",
            "",
            f"**Plan ID:** `{plan.id}`",
            f"**Title:** {plan.title}",
            f"**Steps:** {plan.total_steps}",
            f"**Estimated Duration:** {plan.estimated_duration.total_seconds():.0f}s",
            "",
            f"**Next Actions:**",
            f"• Use `/run {plan.id}` to execute the plan",
            f"• Use `/plan show {plan.id}` to view plan details",
            "",
            "**Steps Overview:**"
        ]
        
        # Add step summary
        for i, step in enumerate(plan.steps[:5], 1):  # Show first 5 steps
            risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🚨"}
            emoji = risk_emoji.get(step.risk_level.value, "❓")
            approval = " ⚠️" if step.requires_approval else ""
            
            content_lines.append(f"  {i}. {emoji} {step.description}{approval}")
        
        if len(plan.steps) > 5:
            content_lines.append(f"  ... and {len(plan.steps) - 5} more steps")
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="green"),
            title=f"📋 Plan Created",
            border_style="green",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _list_plans(self, status_filter: Optional[PlanStatus] = None) -> CommandResult:
        """List existing plans"""
        
        plans = await self.plan_manager.list_plans(status_filter)
        
        if not plans:
            message = "No plans found"
            if status_filter:
                message += f" with status '{status_filter.value}'"
            
            return CommandResult.success_result(
                Panel(Text(message, style="yellow"), title="📋 Plans", border_style="yellow"),
                "rich"
            )
        
        # Create table
        table = Table(title=f"📋 Execution Plans ({len(plans)} found)", show_header=True, header_style="bold magenta")
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Title", style="white", width=30)
        table.add_column("Status", style="green", width=12)
        table.add_column("Steps", justify="right", style="blue", width=8)
        table.add_column("Progress", justify="right", style="yellow", width=10)
        table.add_column("Created", style="dim", width=16)
        
        for plan in plans:
            # Status with emoji
            status_emojis = {
                "draft": "📝",
                "ready": "⚡",
                "running": "🔄",
                "paused": "⏸️",
                "completed": "✅",
                "failed": "❌",
                "cancelled": "🚫"
            }
            
            status_emoji = status_emojis.get(plan["status"], "❓")
            status_text = f"{status_emoji} {plan['status']}"
            
            # Progress calculation
            total = plan["total_steps"]
            completed = plan["completed_steps"]
            progress_pct = (completed / total * 100) if total > 0 else 0
            progress_text = f"{completed}/{total} ({progress_pct:.0f}%)"
            
            # Format date
            created_date = plan["created_at"][:16].replace("T", " ")
            
            table.add_row(
                plan["id"],
                plan["title"][:30] + ("..." if len(plan["title"]) > 30 else ""),
                status_text,
                str(total),
                progress_text,
                created_date
            )
        
        return CommandResult.success_result(table, "rich")
    
    async def _show_plan(self, plan_id: str) -> CommandResult:
        """Show detailed plan information"""
        
        status = await self.plan_manager.get_plan_status(plan_id)
        if not status:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        # Load full plan for step details
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Could not load plan '{plan_id}'")
        
        # Create detailed display
        content_lines = [
            f"**{status['title']}**",
            "",
            f"**ID:** {status['id']}",
            f"**Status:** {status['status']}",
            f"**Created:** {status['timing']['created_at'][:19]}",
        ]
        
        if status['timing']['started_at']:
            content_lines.append(f"**Started:** {status['timing']['started_at'][:19]}")
        
        if status['timing']['completed_at']:
            content_lines.append(f"**Completed:** {status['timing']['completed_at'][:19]}")
        
        # Progress information
        progress = status['progress']
        content_lines.extend([
            "",
            f"**Progress:** {progress['completed']}/{progress['total']} steps ({progress['percentage']:.1f}%)",
            f"**Completed:** {progress['completed']} ✅",
            f"**Failed:** {progress['failed']} ❌",
            f"**Skipped:** {progress['skipped']} ⏭️"
        ])
        
        # Timing information
        if status['timing']['estimated_duration']:
            est_duration = status['timing']['estimated_duration']
            content_lines.append(f"**Estimated Duration:** {est_duration:.0f}s")
        
        if status['timing']['actual_duration']:
            actual_duration = status['timing']['actual_duration']
            content_lines.append(f"**Actual Duration:** {actual_duration:.0f}s")
        
        # Current active steps
        if status['current_steps']:
            content_lines.extend([
                "",
                "**Current Steps:**"
            ])
            for step in status['current_steps']:
                status_emoji = {
                    "running": "🔄",
                    "requires_approval": "⚠️"
                }.get(step['status'], "❓")
                
                approval_text = " (needs approval)" if step['requires_approval'] else ""
                content_lines.append(f"  • {status_emoji} {step['description']}{approval_text}")
        
        # Recent steps (last 5)
        content_lines.extend([
            "",
            "**Recent Steps:**"
        ])
        
        recent_steps = plan.steps[-5:] if len(plan.steps) > 5 else plan.steps
        for step in recent_steps:
            status_emojis = {
                "pending": "⏳",
                "running": "🔄",
                "completed": "✅",
                "failed": "❌",
                "skipped": "⏭️",
                "requires_approval": "⚠️"
            }
            
            emoji = status_emojis.get(step.status.value, "❓")
            risk_indicator = {"high": "🔴", "critical": "🚨"}.get(step.risk_level.value, "")
            
            content_lines.append(f"  • {emoji} {step.description} {risk_indicator}")
            
            if step.error_message:
                content_lines.append(f"    Error: {step.error_message}")
        
        content = "\n".join(content_lines)
        
        # Choose border color based on status
        border_colors = {
            "draft": "yellow",
            "ready": "blue",
            "running": "cyan",
            "completed": "green",
            "failed": "red",
            "cancelled": "dim"
        }
        
        border_color = border_colors.get(status['status'], "white")
        
        result_panel = Panel(
            Text(content, style="white"),
            title=f"📋 Plan Details",
            border_style=border_color,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _delete_plan(self, plan_id: str) -> CommandResult:
        """Delete a plan"""
        
        # Check if plan exists
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        # Check if plan is currently running
        if plan.status == PlanStatus.RUNNING:
            raise CommandError(f"Cannot delete running plan '{plan_id}'. Stop it first with /stop {plan_id}")
        
        # Delete plan files
        plan_file = self.plan_manager.plans_dir / f"{plan_id}.json"
        md_file = self.plan_manager.plans_dir / f"{plan_id}.md"
        
        try:
            if plan_file.exists():
                plan_file.unlink()
            if md_file.exists():
                md_file.unlink()
            
            success_message = f"✅ Plan '{plan_id}' deleted successfully"
            
            return CommandResult.success_result(
                Panel(Text(success_message, style="green"), title="🗑️ Plan Deleted", border_style="green"),
                "rich"
            )
            
        except Exception as e:
            raise CommandError(f"Failed to delete plan '{plan_id}': {str(e)}")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Create and manage autonomous execution plans

Usage:
  /plan                          List all plans
  /plan <objective>              Create plan for objective
  /plan create <objective>       Create plan for objective (explicit)
  /plan list [status]            List plans (optionally filter by status)
  /plan show <plan_id>           Show detailed plan information
  /plan delete <plan_id>         Delete a plan

Plan Status Values:
  draft, ready, running, paused, completed, failed, cancelled

Examples:
  /plan                                    List all existing plans
  /plan "Analyze codebase and fix bugs"   Create plan for objective
  /plan list running                       Show only running plans
  /plan show abc123                        Show details for plan abc123
  /plan delete abc123                      Delete plan abc123

Next Steps:
  • Use /run <plan_id> to execute a plan
  • Use /approve <plan_id> <step_id> to approve pending steps
  • Use /stop <plan_id> to halt execution

Alias: /p"""