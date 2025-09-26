"""
Cost Command for DEILE v4.0
===========================

Command for managing cost tracking, budgets, and financial analytics
with comprehensive reporting and budget management features.

Author: DEILE
Version: 4.0
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.commands.base import DirectCommand
from deile.core.context_manager import ContextManager
from deile.core.exceptions import CommandError
from deile.infrastructure.monitoring.cost_tracker import (
    get_cost_tracker, CostCategory, BudgetPeriod
)

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
    
    def __init__(self):
        super().__init__()
        self.name = "cost"
        self.description = "Cost tracking, budgets, and financial analytics"
        self.aliases = ["costs", "budget", "billing"]
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

    def execute(self, args: List[str]) -> Dict[str, Any]:
        """Execute the cost command"""
        try:
            if not args:
                return self._show_cost_summary()
            
            action = args[0].lower()
            
            if action == "summary":
                days = int(args[1]) if len(args) > 1 else 30
                return self._show_cost_summary(days)
            elif action == "session":
                return self._show_session_costs()
            elif action == "estimate":
                if len(args) < 4:
                    return self._error("Usage: /cost estimate <provider> <model> <tokens>")
                provider, model, tokens = args[1], args[2], int(args[3])
                return self._show_cost_estimate(provider, model, tokens)
            else:
                return self._error(f"Unknown action: {action}")
                
        except ValueError as e:
            return self._error(f"Invalid parameter: {str(e)}")
        except Exception as e:
            logger.error(f"CostCommand execution error: {str(e)}")
            return self._error(f"Command execution failed: {str(e)}")

    def _show_cost_summary(self, days: int = 30) -> Dict[str, Any]:
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
            
            return self._success({
                'content': content,
                'total_amount': float(summary.total_amount),
                'session_cost': float(session_cost),
                'period_days': days,
                'entry_count': summary.entry_count,
                'categories': {k: float(v) for k, v in summary.categories.items()}
            })
            
        except Exception as e:
            return self._error(f"Failed to show cost summary: {str(e)}")

    def _show_session_costs(self) -> Dict[str, Any]:
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
                session_info += f"\n\n📈 **Session is active with costs**"
                style = "green"
            else:
                session_info += "\n\n🎉 **No costs yet in this session!**"
                style = "blue"
            
            content = Panel(Text(session_info, style=style),
                          title="💰 Session Costs",
                          border_style=style)
            
            return self._success({
                'content': content,
                'session_cost': float(session_cost)
            })
            
        except Exception as e:
            return self._error(f"Failed to show session costs: {str(e)}")

    def _show_cost_estimate(self, provider: str, model: str, tokens: int) -> Dict[str, Any]:
        """Show cost estimate for API call"""
        try:
            estimate = self.cost_tracker.get_pricing_estimate(provider, model, tokens)
            
            if 'error' in estimate:
                return self._error(estimate['error'])
            
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
            
            return self._success({
                'content': content,
                'estimate': estimate
            })
            
        except Exception as e:
            return self._error(f"Failed to show cost estimate: {str(e)}")

    def _success(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return success response"""
        return {
            "success": True,
            "command": self.name,
            "data": data,
            "timestamp": datetime.now().isoformat()
        }

    def _error(self, message: str) -> Dict[str, Any]:
        """Return error response"""
        logger.error(f"CostCommand error: {message}")
        return {
            "success": False,
            "command": self.name,
            "error": message,
            "timestamp": datetime.now().isoformat()
        }


# Register the command
from deile.commands.registry import StaticCommandRegistry
StaticCommandRegistry.register("cost", CostCommand)