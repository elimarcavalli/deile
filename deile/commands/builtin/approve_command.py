"""Approve Command - Approve or reject plan steps"""

from typing import Dict, Any, Optional
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...orchestration.plan_manager import get_plan_manager, StepStatus


class ApproveCommand(DirectCommand):
    """Approve or reject plan steps that require manual approval"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="approve",
            description="Approve or reject plan steps that require manual approval.",
            aliases=["a", "approval"]
        )
        super().__init__(config)
        self.plan_manager = get_plan_manager()
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute approve command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show pending approvals
                return await self._show_pending_approvals()
            
            if len(parts) < 2:
                raise CommandError("approve command requires plan ID and step ID: /approve <plan_id> <step_id> [yes|no]")
            
            plan_id = parts[0]
            step_id = parts[1]
            
            # Default to approve, unless explicitly rejected
            approved = True
            if len(parts) > 2:
                approval_response = parts[2].lower()
                if approval_response in ['no', 'n', 'reject', 'deny', 'false']:
                    approved = False
                elif approval_response not in ['yes', 'y', 'approve', 'accept', 'true']:
                    raise CommandError(f"Invalid approval response: {approval_response}. Use 'yes' or 'no'")
            
            return await self._approve_step(plan_id, step_id, approved)
            
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute approve command: {str(e)}")
    
    async def _show_pending_approvals(self) -> CommandResult:
        """Show all steps requiring approval across all plans"""
        
        # Get all plans and check for pending approvals
        all_plans = await self.plan_manager.list_plans()
        pending_approvals = []
        
        for plan_info in all_plans:
            if plan_info['status'] not in ['running', 'paused']:
                continue
            
            plan = await self.plan_manager.load_plan(plan_info['id'])
            if not plan:
                continue
            
            # Find steps requiring approval
            for step in plan.steps:
                if step.status == StepStatus.REQUIRES_APPROVAL:
                    pending_approvals.append({
                        'plan_id': plan.id,
                        'plan_title': plan.title,
                        'step_id': step.id,
                        'step_description': step.description,
                        'tool_name': step.tool_name,
                        'risk_level': step.risk_level.value,
                        'timeout': step.timeout
                    })
        
        if not pending_approvals:
            return CommandResult.success_result(
                Panel(
                    Text("No steps are currently waiting for approval.\n\nAll running plans are executing automatically.", 
                         style="green"),
                    title="✅ No Pending Approvals",
                    border_style="green"
                ),
                "rich"
            )
        
        # Create table of pending approvals
        table = Table(title=f"⚠️ Pending Approvals ({len(pending_approvals)} steps)", show_header=True, header_style="bold yellow")
        table.add_column("Plan", style="cyan", width=12)
        table.add_column("Step ID", style="yellow", width=10)
        table.add_column("Tool", style="green", width=12)
        table.add_column("Description", style="white", width=30)
        table.add_column("Risk", style="red", width=8)
        table.add_column("Action", style="blue", width=20)
        
        for approval in pending_approvals:
            # Risk level emoji
            risk_emoji = {
                "low": "🟢",
                "medium": "🟡",
                "high": "🔴",
                "critical": "🚨"
            }.get(approval['risk_level'], "❓")
            
            # Truncate long descriptions
            description = approval['step_description']
            if len(description) > 30:
                description = description[:27] + "..."
            
            # Plan title truncation
            plan_title = approval['plan_title']
            if len(plan_title) > 12:
                plan_title = plan_title[:9] + "..."
            
            action_text = f"/approve {approval['plan_id']} {approval['step_id']}"
            
            table.add_row(
                plan_title,
                approval['step_id'],
                approval['tool_name'],
                description,
                f"{risk_emoji} {approval['risk_level']}",
                action_text
            )
        
        # Add usage instructions
        usage_panel = Panel(
            Text(
                "Usage:\n"
                "• /approve <plan_id> <step_id>      - Approve step\n"
                "• /approve <plan_id> <step_id> no   - Reject step\n"
                "• /approve <plan_id> <step_id> yes  - Explicitly approve step\n\n"
                "Rejected steps will be skipped and marked as completed.",
                style="dim"
            ),
            title="Usage Instructions",
            border_style="dim"
        )
        
        # Combine table and usage
        content = f"{table}\n\n{usage_panel}"
        
        return CommandResult.success_result(content, "rich")
    
    async def _approve_step(self, plan_id: str, step_id: str, approved: bool) -> CommandResult:
        """Approve or reject a specific step"""
        
        # Load plan to validate step
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        # Find the step
        step = plan.get_step(step_id)
        if not step:
            raise CommandError(f"Step '{step_id}' not found in plan '{plan_id}'")
        
        if step.status != StepStatus.REQUIRES_APPROVAL:
            raise CommandError(f"Step '{step_id}' is not waiting for approval (status: {step.status.value})")
        
        # Perform approval
        success = await self.plan_manager.approve_step(plan_id, step_id, approved)
        if not success:
            raise CommandError(f"Failed to process approval for step '{step_id}'")
        
        # Create result message
        if approved:
            action_text = "approved"
            emoji = "✅"
            style = "green"
            next_action = "The step will continue execution."
        else:
            action_text = "rejected"
            emoji = "⏭️"
            style = "yellow"
            next_action = "The step will be skipped."
        
        # Get updated step info
        updated_plan = await self.plan_manager.load_plan(plan_id)
        if updated_plan:
            updated_step = updated_plan.get_step(step_id)
            if updated_step:
                step = updated_step
        
        content_lines = [
            f"{emoji} **Step {action_text.title()} Successfully**",
            "",
            f"**Plan:** {plan.title}",
            f"**Plan ID:** {plan_id}",
            f"**Step ID:** {step_id}",
            f"**Tool:** {step.tool_name}",
            f"**Description:** {step.description}",
            f"**Risk Level:** {step.risk_level.value}",
            "",
            f"**Status:** {action_text.title()}",
            f"**Next Action:** {next_action}"
        ]
        
        # Show step parameters if approved
        if approved and step.params:
            content_lines.extend([
                "",
                "**Parameters:**"
            ])
            for key, value in step.params.items():
                # Truncate long values
                value_str = str(value)
                if len(value_str) > 50:
                    value_str = value_str[:47] + "..."
                content_lines.append(f"  • {key}: {value_str}")
        
        # Add context about plan status
        plan_status = await self.plan_manager.get_plan_status(plan_id)
        if plan_status:
            remaining_approvals = len([
                s for s in plan.steps 
                if s.status == StepStatus.REQUIRES_APPROVAL and s.id != step_id
            ])
            
            if remaining_approvals > 0:
                content_lines.extend([
                    "",
                    f"⚠️ **{remaining_approvals} more steps** in this plan are waiting for approval.",
                    f"Use `/approve` to see all pending approvals."
                ])
            else:
                content_lines.extend([
                    "",
                    "🚀 **No more approvals needed** for this plan.",
                    "Execution will continue automatically."
                ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style=style),
            title=f"⚡ Step {action_text.title()}",
            border_style=style,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Approve or reject plan steps that require manual approval

Usage:
  /approve                         Show all pending approvals
  /approve <plan_id> <step_id>     Approve step (default)
  /approve <plan_id> <step_id> yes Explicitly approve step
  /approve <plan_id> <step_id> no  Reject/skip step

Approval Responses:
  yes, y, approve, accept, true    - Approve the step
  no, n, reject, deny, false       - Reject/skip the step

Examples:
  /approve                         List all steps waiting for approval
  /approve abc123 def456           Approve step def456 in plan abc123
  /approve abc123 def456 no        Reject step def456 (will be skipped)

Step Behavior:
  • Approved steps continue normal execution
  • Rejected steps are marked as skipped
  • High-risk steps always require manual approval
  • Low-risk steps can be auto-approved (depending on /run options)

Plan Status:
  • Plans pause execution when steps require approval
  • Multiple steps may require approval simultaneously
  • Use /run <plan_id> to see overall plan progress

Related Commands:
  • /run <plan_id> - Execute plan
  • /plan show <plan_id> - Show plan details
  • /stop <plan_id> - Stop plan execution

Aliases: /a, /approval"""