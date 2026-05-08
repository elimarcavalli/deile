"""
Cost Command for DEILE
===========================

Command for managing cost tracking, budgets, and financial analytics
with comprehensive reporting and budget management features.

Author: DEILE
"""

import logging
from datetime import datetime, timedelta
from typing import List

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.commands.base import CommandResult, DirectCommand
from deile.core.context_manager import ContextManager
from deile.infrastructure.monitoring.cost_tracker import get_cost_tracker

logger = logging.getLogger(__name__)


class CostCommand(DirectCommand):
    """
    Command for comprehensive cost management and analytics

    Features:
    - Current session and total cost tracking
    - Budget management and alerts
    - Cost forecasting and analysis
    - Detailed cost breakdowns by category
    - Export capabilities
    - Real-time cost monitoring
    """

    cli_flag = "--cost"
    cli_help = "Show accumulated session costs and exit."
    cli_requires_provider = False

    def __init__(self):
        super().__init__()
        self.config.description = "Cost tracking, budgets, and financial analytics"
        self.help_text = """
Cost Command - Financial Management and Analytics

USAGE:
    /cost [action] [options]

ACTIONS:
    summary [days]           Show cost summary (default: 30 days)
    session                  Show current session costs
    categories               Show costs by category
    budget list              List all budget limits
    budget set <category> <period> <amount>  Set budget limit
    budget check             Check budget status
    forecast [days]          Forecast costs (default: 7 days)
    export [format] [days]   Export cost data (json, csv)
    estimate <provider> <model> <tokens>  Estimate API call cost
    top [count]              Show top expenses
    alerts                   Show budget alerts
    
PERIODS:
    daily, weekly, monthly, yearly

CATEGORIES:
    api_calls, compute, storage, network, model_usage, 
    sandbox, infrastructure, external_services

EXAMPLES:
    /cost summary                           # Last 30 days summary
    /cost summary 7                         # Last 7 days
    /cost session                           # Current session costs
    /cost budget set api_calls monthly 100  # Set $100/month for API calls
    /cost forecast 14                       # 14-day forecast
    /cost export json 90                    # Export last 90 days as JSON
    /cost estimate gemini pro 5000          # Estimate cost for 5000 tokens
"""
        
        self.cost_tracker = get_cost_tracker()
        self.context_manager = ContextManager()

    async def execute(self, context=None) -> "CommandResult":
        """Execute the cost command.

        Reads :class:`CommandContext.args`, dispatches to a sub-action, and
        returns a :class:`CommandResult`. The internal ``_show_*`` helpers
        return ``CommandResult`` directly.
        """

        args_str = getattr(context, "args", "") or "" if context is not None else ""
        args_list: List[str] = args_str.split() if args_str else []

        try:
            if not args_list:
                return self._show_cost_summary()

            action = args_list[0].lower()

            if action == "summary":
                days = int(args_list[1]) if len(args_list) > 1 else 30
                return self._show_cost_summary(days)
            if action == "session":
                return self._show_session_costs()
            if action == "estimate":
                if len(args_list) < 4:
                    return CommandResult.error_result(
                        "Usage: /cost estimate <provider> <model> <tokens>"
                    )
                provider, model, tokens = args_list[1], args_list[2], int(args_list[3])
                return self._show_cost_estimate(provider, model, tokens)
            return CommandResult.error_result(f"Unknown action: {action}")

        except ValueError as exc:
            return CommandResult.error_result(f"Invalid parameter: {exc}")
        except Exception as exc:
            logger.error("CostCommand execution error: %s", exc)
            return CommandResult.error_result(
                f"Command execution failed: {exc}", error=exc
            )

    def _show_cost_summary(self, days: int = 30) -> "CommandResult":
        """Show comprehensive cost summary"""
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)

            # Get cost summary
            summary = self.cost_tracker.get_cost_summary(start_time, end_time)
            session_cost = self.cost_tracker.get_current_session_cost()
            
            # Create main summary table
            summary_table = Table(title=f"💰 Cost Summary ({days} days)", show_header=True, header_style="bold cyan")
            summary_table.add_column("Metric", style="white", width=20)
            summary_table.add_column("Value", style="green", width=20)
            summary_table.add_column("Details", style="dim", width=30)
            
            summary_table.add_row(
                "Total Spent",
                f"${summary.total_amount:.4f}",
                f"{summary.entry_count} transactions"
            )
            summary_table.add_row(
                "Daily Average",
                f"${summary.total_amount / days:.4f}",
                f"Based on {days} days"
            )
            summary_table.add_row(
                "Current Session",
                f"${session_cost:.4f}",
                "Active session cost"
            )
            
            if summary.total_amount > 0:
                summary_table.add_row(
                    "Avg per Transaction",
                    f"${summary.total_amount / summary.entry_count:.6f}",
                    "Per cost entry"
                )
            
            # Category breakdown
            if summary.categories:
                category_table = Table(title="📊 Costs by Category", show_header=True, header_style="bold yellow")
                category_table.add_column("Category", style="cyan", width=20)
                category_table.add_column("Amount", style="green", width=15)
                category_table.add_column("Percentage", style="white", width=15)
                category_table.add_column("Visual", style="blue", width=20)
                
                for category, amount in sorted(summary.categories.items(), key=lambda x: x[1], reverse=True):
                    percentage = (amount / summary.total_amount * 100) if summary.total_amount > 0 else 0
                    bar_width = int(percentage / 5)  # Scale for display
                    visual_bar = "█" * min(bar_width, 20)
                    
                    category_table.add_row(
                        category,
                        f"${amount:.4f}",
                        f"{percentage:.1f}%",
                        visual_bar
                    )
                
                content = Group(summary_table, "", category_table)
            else:
                no_data = Panel(
                    Text("No cost data found for the selected period.", style="yellow"),
                    title="📊 Categories",
                    border_style="yellow"
                )
                content = Group(summary_table, "", no_data)
            
            return CommandResult.success_result(
                content,
                "rich",
                total_amount=float(summary.total_amount),
                session_cost=float(session_cost),
                period_days=days,
                entry_count=summary.entry_count,
                categories={k: float(v) for k, v in summary.categories.items()},
            )

        except Exception as exc:
            logger.error("Failed to show cost summary: %s", exc)
            return CommandResult.error_result(
                f"Failed to show cost summary: {exc}", error=exc
            )

    def _show_session_costs(self) -> "CommandResult":
        """Show current session costs"""
        try:
            session_cost = self.cost_tracker.get_current_session_cost()
            
            # Create session info panel
            session_info = (
                f"💰 **Current Session Cost**: ${session_cost:.6f}\n\n"
                "This represents the cost of API calls and resource usage\n"
                "in your current DEILE session.\n\n"
                "📊 **What's included**:\n"
                "• API calls to language models\n"
                "• Compute resource usage\n" 
                "• Sandbox container costs\n"
                "• Network and storage usage\n\n"
                "💡 **Cost will reset when you start a new session**"
            )
            
            if session_cost > 0:
                session_info += "\n\n📈 **Session is active with costs**"
                style = "green"
            else:
                session_info += "\n\n🎉 **No costs yet in this session!**"
                style = "blue"
            
            content = Panel(Text(session_info, style=style),
                          title="💰 Session Costs",
                          border_style=style)
            
            return CommandResult.success_result(
                content, "rich", session_cost=float(session_cost)
            )

        except Exception as exc:
            logger.error("Failed to show session costs: %s", exc)
            return CommandResult.error_result(
                f"Failed to show session costs: {exc}", error=exc
            )

    def _show_cost_estimate(self, provider: str, model: str, tokens: int) -> "CommandResult":
        """Show cost estimate for API call"""
        try:
            estimate = self.cost_tracker.get_pricing_estimate(provider, model, tokens)

            if "error" in estimate:
                return CommandResult.error_result(estimate["error"])
            
            # Create estimate table
            estimate_table = Table(title="💰 Cost Estimate", show_header=True, header_style="bold cyan")
            estimate_table.add_column("Component", style="white", width=20)
            estimate_table.add_column("Tokens", style="yellow", width=15)
            estimate_table.add_column("Cost", style="green", width=15)
            estimate_table.add_column("Rate", style="dim", width=20)
            
            estimate_table.add_row(
                "Input Tokens",
                f"{estimate['estimated_input_tokens']:,}",
                f"${estimate['estimated_input_cost']:.6f}",
                f"${estimate['estimated_input_cost'] / estimate['estimated_input_tokens'] * 1000:.4f}/1K" if estimate['estimated_input_tokens'] > 0 else "N/A"
            )
            
            estimate_table.add_row(
                "Output Tokens", 
                f"{estimate['estimated_output_tokens']:,}",
                f"${estimate['estimated_output_cost']:.6f}",
                f"${estimate['estimated_output_cost'] / estimate['estimated_output_tokens'] * 1000:.4f}/1K" if estimate['estimated_output_tokens'] > 0 else "N/A"
            )
            
            estimate_table.add_row(
                "**Total**",
                f"**{estimate['estimated_total_tokens']:,}**",
                f"**${estimate['estimated_total_cost']:.6f}**",
                f"**${estimate['cost_per_token'] * 1000:.4f}/1K**"
            )
            
            # Estimate details
            details_text = (
                f"🔍 **Estimate Details**\n\n"
                f"Provider: {estimate['provider']}\n"
                f"Model: {estimate['model']}\n"
                f"Currency: {estimate['currency']}\n\n"
                f"📊 **Token Distribution**\n"
                f"• Input: {estimate['estimated_input_tokens']:,} tokens (70%)\n"
                f"• Output: {estimate['estimated_output_tokens']:,} tokens (30%)\n\n"
                f"💡 **Note**: This is an estimate based on typical usage patterns.\n"
                f"Actual costs may vary depending on the specific request."
            )
            
            details_panel = Panel(
                Text(details_text, style="blue"),
                title="🔍 Details",
                border_style="blue"
            )
            
            content = Group(estimate_table, "", details_panel)
            
            return CommandResult.success_result(
                content, "rich", estimate=estimate
            )

        except Exception as exc:
            logger.error("Failed to show cost estimate: %s", exc)
            return CommandResult.error_result(
                f"Failed to show cost estimate: {exc}", error=exc
            )


# Register the command
from deile.commands.registry import StaticCommandRegistry  # noqa: E402

StaticCommandRegistry.register("cost", CostCommand)
