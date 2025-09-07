"""Run Command - Execute plans autonomously"""

from typing import Dict, Any, Optional
import asyncio
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.live import Live

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...orchestration.plan_manager import get_plan_manager, PlanStatus


class RunCommand(DirectCommand):
    """Execute execution plans autonomously"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="run",
            description="Execute execution plans autonomously.",
            aliases=["r", "execute"]
        )
        super().__init__(config)
        self.plan_manager = get_plan_manager()
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute run command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show running plans
                return await self._show_running_plans()
            
            plan_id = parts[0]
            
            # Parse options
            auto_approve_low_risk = True
            dry_run = False
            
            for part in parts[1:]:
                if part == "--no-auto-approve":
                    auto_approve_low_risk = False
                elif part == "--dry-run":
                    dry_run = True
                elif part.startswith("--"):
                    raise CommandError(f"Unknown option: {part}")
            
            if dry_run:
                return await self._dry_run_plan(plan_id)
            else:
                return await self._execute_plan(plan_id, auto_approve_low_risk)
            
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute run command: {str(e)}")
    
    async def _show_running_plans(self) -> CommandResult:
        """Show currently running plans"""
        
        plans = await self.plan_manager.list_plans(PlanStatus.RUNNING)
        
        if not plans:
            return CommandResult.success_result(
                Panel(
                    Text("No plans are currently running.\n\nUse '/plan list' to see all plans or '/plan create <objective>' to create a new plan.", 
                         style="yellow"),
                    title="🔄 Running Plans",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create table of running plans
        table = Table(title="🔄 Currently Running Plans", show_header=True, header_style="bold cyan")
        table.add_column("Plan ID", style="cyan", width=10)
        table.add_column("Title", style="white", width=30)
        table.add_column("Progress", style="green", width=15)
        table.add_column("Current Step", style="yellow", width=25)
        table.add_column("Started", style="dim", width=16)
        
        for plan in plans:
            # Get detailed status
            status = await self.plan_manager.get_plan_status(plan["id"])
            if not status:
                continue
            
            progress = status['progress']
            progress_text = f"{progress['completed']}/{progress['total']} ({progress['percentage']:.0f}%)"
            
            # Current step info
            current_steps = status.get('current_steps', [])
            if current_steps:
                current_step = current_steps[0]['description'][:25]
                if len(current_steps[0]['description']) > 25:
                    current_step += "..."
            else:
                current_step = "No active step"
            
            started_at = status['timing']['started_at'][:16].replace("T", " ") if status['timing']['started_at'] else "Unknown"
            
            table.add_row(
                plan["id"],
                plan["title"][:30] + ("..." if len(plan["title"]) > 30 else ""),
                progress_text,
                current_step,
                started_at
            )
        
        return CommandResult.success_result(table, "rich")
    
    async def _dry_run_plan(self, plan_id: str) -> CommandResult:
        """Show what would be executed without running"""
        
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        if plan.status not in [PlanStatus.DRAFT, PlanStatus.READY]:
            raise CommandError(f"Plan '{plan_id}' cannot be dry-run (status: {plan.status.value})")
        
        # Create dry run analysis
        content_lines = [
            f"🔍 **Dry Run Analysis for Plan: {plan.title}**",
            "",
            f"**Plan ID:** {plan.id}",
            f"**Total Steps:** {plan.total_steps}",
            f"**Estimated Duration:** {plan.estimated_duration.total_seconds():.0f}s",
            "",
            "**Execution Order:**"
        ]
        
        # Analyze execution order and dependencies
        ready_steps = plan.get_next_steps()
        step_queue = ready_steps.copy()
        executed_steps = []
        step_order = 1
        
        while step_queue:
            current_steps = step_queue[:plan.max_concurrent_steps]
            step_queue = step_queue[plan.max_concurrent_steps:]
            
            for step in current_steps:
                # Risk indicators
                risk_emoji = {
                    "low": "🟢",
                    "medium": "🟡", 
                    "high": "🔴",
                    "critical": "🚨"
                }.get(step.risk_level.value, "❓")
                
                approval_text = " ⚠️ (needs approval)" if step.requires_approval else ""
                deps_text = f" (depends on: {', '.join(step.depends_on)})" if step.depends_on else ""
                
                content_lines.append(
                    f"  {step_order}. {risk_emoji} **{step.tool_name}** - {step.description}{approval_text}{deps_text}"
                )
                content_lines.append(f"     Timeout: {step.timeout}s")
                
                executed_steps.append(step.id)
                step_order += 1
            
            # Find next ready steps
            for step in plan.steps:
                if step.id in executed_steps or step in step_queue:
                    continue
                
                # Check if dependencies are met
                deps_met = all(dep_id in [s.id for s in executed_steps] for dep_id in step.depends_on)
                if deps_met:
                    step_queue.append(step)
        
        # Warnings and recommendations
        content_lines.extend([
            "",
            "**Analysis:**"
        ])
        
        high_risk_steps = [s for s in plan.steps if s.risk_level.value in ["high", "critical"]]
        approval_steps = [s for s in plan.steps if s.requires_approval]
        
        if high_risk_steps:
            content_lines.append(f"  ⚠️ {len(high_risk_steps)} high-risk steps require attention")
        
        if approval_steps:
            content_lines.append(f"  🔐 {len(approval_steps)} steps require manual approval")
        
        estimated_approvals = len([s for s in plan.steps if s.requires_approval and s.risk_level.value != "low"])
        if estimated_approvals > 0:
            content_lines.append(f"  ⏸️ Execution may pause {estimated_approvals} times for approvals")
        
        content_lines.extend([
            "",
            "**To execute this plan:**",
            f"  `/run {plan_id}` - Execute with auto-approval for low-risk steps",
            f"  `/run {plan_id} --no-auto-approve` - Require approval for all steps"
        ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="white"),
            title="🔍 Dry Run Analysis",
            border_style="blue",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _execute_plan(self, plan_id: str, auto_approve_low_risk: bool) -> CommandResult:
        """Execute a plan with progress tracking"""
        
        # Validate plan
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        if plan.status not in [PlanStatus.DRAFT, PlanStatus.READY, PlanStatus.PAUSED]:
            raise CommandError(f"Plan '{plan_id}' cannot be executed (status: {plan.status.value})")
        
        # Create progress display
        progress = Progress(
            TextColumn("[bold blue]Executing Plan:", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            TextColumn("{task.description}"),
            "•",
            TimeElapsedColumn(),
        )
        
        # Start execution with live progress
        try:
            with Live(progress, refresh_per_second=2) as live:
                task = progress.add_task(f"[cyan]Starting {plan.title}...", total=plan.total_steps)
                
                # Execute plan asynchronously and update progress
                execution_result = await self._execute_with_progress(
                    plan_id, auto_approve_low_risk, progress, task
                )
            
            # Show final results
            return await self._format_execution_result(execution_result)
            
        except KeyboardInterrupt:
            # User interrupted execution
            await self.plan_manager.stop_plan(plan_id)
            
            return CommandResult.success_result(
                Panel(
                    Text(f"⏹️ Plan execution stopped by user.\n\nPlan '{plan_id}' has been cancelled.", style="yellow"),
                    title="Execution Interrupted",
                    border_style="yellow"
                ),
                "rich"
            )
        
        except Exception as e:
            raise CommandError(f"Failed to execute plan '{plan_id}': {str(e)}")
    
    async def _execute_with_progress(self, plan_id: str, auto_approve_low_risk: bool,
                                   progress: Progress, task) -> Dict[str, Any]:
        """Execute plan with progress updates"""
        
        # Start execution task
        execution_task = asyncio.create_task(
            self.plan_manager.execute_plan(plan_id, auto_approve_low_risk)
        )
        
        # Monitor progress
        while not execution_task.done():
            # Get current plan status
            status = await self.plan_manager.get_plan_status(plan_id)
            if status:
                completed = status['progress']['completed']
                total = status['progress']['total']
                
                # Update progress
                progress.update(task, completed=completed, total=total)
                
                # Update description with current step
                current_steps = status.get('current_steps', [])
                if current_steps:
                    desc = current_steps[0]['description'][:30]
                    if len(current_steps[0]['description']) > 30:
                        desc += "..."
                    
                    if current_steps[0]['status'] == 'requires_approval':
                        desc = f"⚠️ {desc} (needs approval)"
                    
                    progress.update(task, description=f"[cyan]{desc}")
                else:
                    progress.update(task, description="[cyan]Processing...")
            
            await asyncio.sleep(0.5)
        
        # Get final result
        return await execution_task
    
    async def _format_execution_result(self, result: Dict[str, Any]) -> CommandResult:
        """Format the execution result"""
        
        plan_summary = result.get('plan_summary', {})
        execution_log = result.get('execution_log', [])
        final_stats = result.get('final_stats', {})
        
        # Determine overall status
        status = plan_summary.get('status', 'unknown')
        success = status == 'completed'
        
        # Status emoji and color
        if success:
            status_emoji = "✅"
            border_color = "green"
            status_style = "green"
        elif status == 'failed':
            status_emoji = "❌"
            border_color = "red"
            status_style = "red"
        elif status == 'cancelled':
            status_emoji = "🚫"
            border_color = "yellow"
            status_style = "yellow"
        else:
            status_emoji = "❓"
            border_color = "blue"
            status_style = "blue"
        
        # Create result content
        content_lines = [
            f"{status_emoji} **Plan Execution {status.title()}**",
            "",
            f"**Plan:** {plan_summary.get('title', 'Unknown')}",
            f"**ID:** {plan_summary.get('id', 'Unknown')}",
            f"**Duration:** {plan_summary.get('duration', 0):.1f}s",
            "",
            "**Results:**",
            f"  • Total Steps: {plan_summary.get('total_steps', 0)}",
            f"  • Completed: {final_stats.get('completed', 0)} ✅",
            f"  • Failed: {final_stats.get('failed', 0)} ❌",
            f"  • Skipped: {final_stats.get('skipped', 0)} ⏭️"
        ]
        
        # Add execution summary
        if execution_log:
            content_lines.extend([
                "",
                "**Execution Log (last 5 events):**"
            ])
            
            # Show last 5 events
            recent_events = execution_log[-5:] if len(execution_log) > 5 else execution_log
            
            for event in recent_events:
                action = event.get('action', 'unknown')
                
                if action == 'completed':
                    duration = event.get('duration', 0)
                    content_lines.append(f"  ✅ Step completed in {duration:.1f}s")
                elif action == 'failed':
                    error = event.get('error', 'Unknown error')[:50]
                    content_lines.append(f"  ❌ Step failed: {error}")
                elif action == 'waiting_approval':
                    steps = event.get('steps', [])
                    content_lines.append(f"  ⚠️ Waited for approval of {len(steps)} steps")
                elif action == 'error':
                    error = event.get('error', 'Unknown error')[:50]
                    content_lines.append(f"  💥 Execution error: {error}")
        
        # Add recommendations
        content_lines.append("")
        
        if success:
            content_lines.extend([
                "🎉 **Plan completed successfully!**",
                "",
                "Check the ARTIFACTS/ directory for any generated files."
            ])
        elif status == 'failed':
            content_lines.extend([
                "**Next Steps:**",
                f"• Use `/plan show {plan_summary.get('id')}` to see detailed failure information",
                "• Fix any issues and create a new plan",
                "• Check logs in RUNS/ directory for detailed execution data"
            ])
        elif status == 'cancelled':
            content_lines.extend([
                "**Plan was cancelled.**",
                f"• Use `/plan show {plan_summary.get('id')}` to see progress made",
                f"• You can delete this plan with `/plan delete {plan_summary.get('id')}`"
            ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style=status_style),
            title=f"🚀 Execution Result",
            border_style=border_color,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Execute execution plans autonomously

Usage:
  /run                              Show currently running plans
  /run <plan_id>                    Execute plan with auto-approval for low-risk steps
  /run <plan_id> --no-auto-approve  Execute plan requiring approval for all steps
  /run <plan_id> --dry-run          Show execution analysis without running

Options:
  --no-auto-approve    Require manual approval for all steps
  --dry-run           Show what would be executed without running

Examples:
  /run                          Show running plans
  /run abc123                   Execute plan abc123
  /run abc123 --dry-run         Analyze plan abc123 execution
  /run abc123 --no-auto-approve Execute with manual approval required

Plan Execution:
  • Low-risk steps are auto-approved by default
  • High-risk steps require manual approval with /approve
  • Use Ctrl+C to interrupt execution
  • Progress is shown in real-time
  • Results are saved to RUNS/ directory

Related Commands:
  • /plan create <objective> - Create a new plan
  • /approve <plan_id> <step_id> - Approve pending steps
  • /stop <plan_id> - Stop running plan

Aliases: /r, /execute"""