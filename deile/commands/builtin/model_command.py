"""
Model Command for DEILE v4.0
=============================

Command for intelligent model management, switching, and performance analytics
with comprehensive monitoring and optimization features.

Author: DEILE
Version: 4.0
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, TaskID
from rich.columns import Columns

from deile.commands.base import BaseCommand
from deile.core.context_manager import ContextManager
from deile.core.exceptions import CommandError
from deile.core.models.model_switcher import ModelSwitcher, ModelInfo, ModelConfig
from deile.infrastructure.monitoring.cost_tracker import get_cost_tracker

logger = logging.getLogger(__name__)


class ModelCommand(BaseCommand):
    """
    Command for comprehensive model management and analytics
    
    Features:
    - Model listing with performance metrics
    - Intelligent model switching (manual and automatic)
    - Performance analytics and benchmarking
    - Health monitoring and status tracking
    - Cost optimization and budget integration
    - Model comparison and capability analysis
    - Auto-selection with configurable criteria
    """
    
    def __init__(self):
        super().__init__()
        self.name = "model"
        self.description = "Model management, switching, and performance analytics"
        self.aliases = ["models", "switch", "ai"]
        self.help_text = """
Model Command - AI Model Management and Analytics

USAGE:
    /model [action] [options]

ACTIONS:
    list [provider]              List available models (all or specific provider)
    current                      Show current active model
    switch <model_name>          Switch to specific model
    auto [criteria]              Enable auto-selection (performance, cost, balanced)
    manual                       Disable auto-selection
    status                       Show model health and status
    performance [days]           Show performance analytics (default: 7 days)
    compare <model1> <model2>    Compare two models
    benchmark [provider]         Run model benchmark tests
    providers                    List all available providers
    capabilities <model>         Show model capabilities and limits
    history [count]              Show recent model switches (default: 10)
    reset                        Reset model performance data
    config                       Show model configuration
    
PROVIDERS:
    gemini, openai, anthropic, cohere, huggingface, local

CRITERIA:
    performance                  Prioritize response time and quality
    cost                         Prioritize lowest cost
    balanced                     Balance cost and performance (default)
    reliability                  Prioritize uptime and success rate

EXAMPLES:
    /model list                              # Show all available models
    /model list gemini                       # Show only Gemini models
    /model current                           # Show current model
    /model switch gemini-pro                 # Switch to Gemini Pro
    /model auto performance                  # Enable auto-selection for performance
    /model performance 30                    # Show 30-day performance analytics
    /model compare gemini-pro gpt-4          # Compare two models
    /model benchmark                         # Run benchmark on all models
    /model capabilities gpt-4                # Show GPT-4 capabilities
"""
        
        self.model_switcher = ModelSwitcher()
        self.cost_tracker = get_cost_tracker()
        self.context_manager = ContextManager()
    
    def execute(self, args: List[str]) -> Dict[str, Any]:
        """Execute the model command"""
        try:
            if not args:
                return self._show_model_status()
            
            action = args[0].lower()
            
            if action == "list":
                provider = args[1] if len(args) > 1 else None
                return self._list_models(provider)
            elif action == "current":
                return self._show_current_model()
            elif action == "switch":
                if len(args) < 2:
                    return self._error("Usage: /model switch <model_name>")
                model_name = args[1]
                return self._switch_model(model_name)
            elif action == "auto":
                criteria = args[1] if len(args) > 1 else "balanced"
                return self._enable_auto_selection(criteria)
            elif action == "manual":
                return self._disable_auto_selection()
            elif action == "status":
                return self._show_model_status()
            elif action == "performance":
                days = int(args[1]) if len(args) > 1 else 7
                return self._show_performance_analytics(days)
            elif action == "compare":
                if len(args) < 3:
                    return self._error("Usage: /model compare <model1> <model2>")
                model1, model2 = args[1], args[2]
                return self._compare_models(model1, model2)
            elif action == "benchmark":
                provider = args[1] if len(args) > 1 else None
                return self._run_benchmark(provider)
            elif action == "providers":
                return self._list_providers()
            elif action == "capabilities":
                if len(args) < 2:
                    return self._error("Usage: /model capabilities <model_name>")
                model_name = args[1]
                return self._show_capabilities(model_name)
            elif action == "history":
                count = int(args[1]) if len(args) > 1 else 10
                return self._show_switch_history(count)
            elif action == "reset":
                return self._reset_performance_data()
            elif action == "config":
                return self._show_model_config()
            else:
                return self._error(f"Unknown action: {action}")
                
        except ValueError as e:
            return self._error(f"Invalid parameter: {str(e)}")
        except Exception as e:
            logger.error(f"ModelCommand execution error: {str(e)}")
            return self._error(f"Command execution failed: {str(e)}")
    
    def _list_models(self, provider: Optional[str] = None) -> Dict[str, Any]:
        """List available models with performance metrics"""
        try:
            models = self.model_switcher.list_models(provider)
            current_model = self.model_switcher.get_current_model()
            
            if not models:
                no_models = Panel(
                    Text("No models available." + (f" for provider '{provider}'" if provider else ""), 
                         style="yellow"),
                    title="🤖 Models",
                    border_style="yellow"
                )
                return self._success({'content': no_models, 'models': []})
            
            # Create models table
            models_table = Table(
                title=f"🤖 Available Models{f' ({provider})' if provider else ''}", 
                show_header=True, 
                header_style="bold cyan"
            )
            models_table.add_column("Model", style="white", width=20)
            models_table.add_column("Provider", style="cyan", width=12)
            models_table.add_column("Status", style="green", width=10)
            models_table.add_column("Avg Response", style="yellow", width=12)
            models_table.add_column("Success Rate", style="green", width=12)
            models_table.add_column("Cost/1K", style="blue", width=12)
            
            for model in sorted(models, key=lambda x: (x.provider, x.name)):
                status_icon = "🟢" if model.name == current_model.name else "⚪"
                status_text = "ACTIVE" if model.name == current_model.name else "available"
                
                models_table.add_row(
                    model.name,
                    model.provider,
                    f"{status_icon} {status_text}",
                    f"{model.avg_response_time:.2f}s" if model.avg_response_time else "N/A",
                    f"{model.success_rate:.1f}%" if model.success_rate else "N/A",
                    f"${model.cost_per_1k_tokens:.4f}" if model.cost_per_1k_tokens else "N/A"
                )
            
            # Model summary stats
            total_models = len(models)
            active_providers = len(set(model.provider for model in models))
            
            summary_text = (
                f"📊 **Summary**\n\n"
                f"Total Models: {total_models}\n"
                f"Active Providers: {active_providers}\n"
                f"Current Model: {current_model.name} ({current_model.provider})\n"
                f"Auto-Selection: {'Enabled' if self.model_switcher.is_auto_selection_enabled() else 'Disabled'}"
            )
            
            summary_panel = Panel(
                Text(summary_text, style="blue"),
                title="📊 Summary",
                border_style="blue"
            )
            
            content = Group(models_table, "", summary_panel)
            
            return self._success({
                'content': content,
                'models': [model.to_dict() for model in models],
                'current_model': current_model.name,
                'total_count': total_models
            })
            
        except Exception as e:
            return self._error(f"Failed to list models: {str(e)}")

    def _show_current_model(self) -> Dict[str, Any]:
        """Show current active model with detailed information"""
        try:
            current_model = self.model_switcher.get_current_model()
            performance = self.model_switcher.get_model_performance(current_model.name)
            
            # Current model info
            model_info = (
                f"🤖 **Current Model**: {current_model.name}\n\n"
                f"🏢 **Provider**: {current_model.provider}\n"
                f"📊 **Model Type**: {current_model.model_type}\n"
                f"🎯 **Capabilities**: {', '.join(current_model.capabilities)}\n\n"
                f"⚡ **Performance Metrics**:\n"
                f"• Average Response Time: {performance.get('avg_response_time', 0):.2f}s\n"
                f"• Success Rate: {performance.get('success_rate', 0):.1f}%\n"
                f"• Total Requests: {performance.get('total_requests', 0):,}\n"
                f"• Failed Requests: {performance.get('failed_requests', 0):,}\n\n"
                f"💰 **Cost Information**:\n"
                f"• Cost per 1K tokens: ${current_model.cost_per_1k_tokens:.4f}\n"
                f"• Input token cost: ${current_model.input_cost_per_token * 1000:.4f}/1K\n"
                f"• Output token cost: ${current_model.output_cost_per_token * 1000:.4f}/1K\n\n"
                f"📈 **Limits**:\n"
                f"• Max tokens: {current_model.max_tokens:,}\n"
                f"• Context window: {current_model.context_window:,}\n"
                f"• Rate limit: {current_model.rate_limit} req/min"
            )
            
            if self.model_switcher.is_auto_selection_enabled():
                model_info += f"\n\n🔄 **Auto-Selection**: Enabled ({self.model_switcher.auto_selection_criteria})"
            else:
                model_info += "\n\n🔄 **Auto-Selection**: Disabled (Manual mode)"
            
            content = Panel(
                Text(model_info, style="green"),
                title="🤖 Current Model",
                border_style="green"
            )
            
            return self._success({
                'content': content,
                'current_model': current_model.to_dict(),
                'performance': performance
            })
            
        except Exception as e:
            return self._error(f"Failed to show current model: {str(e)}")

    def _switch_model(self, model_name: str) -> Dict[str, Any]:
        """Switch to specific model"""
        try:
            success = self.model_switcher.switch_model(model_name)
            
            if success:
                new_model = self.model_switcher.get_current_model()
                
                switch_info = (
                    f"✅ **Model Switch Successful**\n\n"
                    f"🤖 **New Model**: {new_model.name}\n"
                    f"🏢 **Provider**: {new_model.provider}\n"
                    f"⏰ **Switched at**: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"💡 **Quick Info**:\n"
                    f"• Capabilities: {', '.join(new_model.capabilities)}\n"
                    f"• Max tokens: {new_model.max_tokens:,}\n"
                    f"• Cost per 1K: ${new_model.cost_per_1k_tokens:.4f}"
                )
                
                content = Panel(
                    Text(switch_info, style="green"),
                    title="✅ Model Switched",
                    border_style="green"
                )
                
                return self._success({
                    'content': content,
                    'switched_to': model_name,
                    'new_model': new_model.to_dict()
                })
            else:
                return self._error(f"Failed to switch to model '{model_name}'. Model may not be available.")
                
        except Exception as e:
            return self._error(f"Failed to switch model: {str(e)}")

    def _enable_auto_selection(self, criteria: str) -> Dict[str, Any]:
        """Enable automatic model selection"""
        try:
            valid_criteria = ["performance", "cost", "balanced", "reliability"]
            if criteria not in valid_criteria:
                return self._error(f"Invalid criteria '{criteria}'. Valid options: {', '.join(valid_criteria)}")
            
            self.model_switcher.enable_auto_selection(criteria)
            
            auto_info = (
                f"🔄 **Auto-Selection Enabled**\n\n"
                f"📊 **Criteria**: {criteria.title()}\n"
                f"⏰ **Enabled at**: {datetime.now().strftime('%H:%M:%S')}\n\n"
                f"💡 **What this means**:\n"
            )
            
            if criteria == "performance":
                auto_info += "• Models will be selected for fastest response times\n• Quality and accuracy will be prioritized"
            elif criteria == "cost":
                auto_info += "• Models with lowest cost per token will be preferred\n• Budget optimization will be prioritized"
            elif criteria == "balanced":
                auto_info += "• Balance between cost and performance\n• Good general-purpose selection"
            elif criteria == "reliability":
                auto_info += "• Models with highest success rates\n• Stability and uptime will be prioritized"
            
            auto_info += f"\n\n🤖 The system will now automatically select the best model based on {criteria} criteria."
            
            content = Panel(
                Text(auto_info, style="blue"),
                title="🔄 Auto-Selection",
                border_style="blue"
            )
            
            return self._success({
                'content': content,
                'criteria': criteria,
                'enabled': True
            })
            
        except Exception as e:
            return self._error(f"Failed to enable auto-selection: {str(e)}")

    def _disable_auto_selection(self) -> Dict[str, Any]:
        """Disable automatic model selection"""
        try:
            self.model_switcher.disable_auto_selection()
            current_model = self.model_switcher.get_current_model()
            
            manual_info = (
                f"🎯 **Manual Mode Enabled**\n\n"
                f"🤖 **Current Model**: {current_model.name}\n"
                f"⏰ **Disabled at**: {datetime.now().strftime('%H:%M:%S')}\n\n"
                f"💡 **Manual mode active**:\n"
                f"• Model will stay fixed until manually switched\n"
                f"• Use '/model switch <model>' to change models\n"
                f"• Use '/model auto <criteria>' to re-enable auto-selection\n\n"
                f"🔧 **Current model will remain**: {current_model.name}"
            )
            
            content = Panel(
                Text(manual_info, style="yellow"),
                title="🎯 Manual Mode",
                border_style="yellow"
            )
            
            return self._success({
                'content': content,
                'manual_mode': True,
                'current_model': current_model.name
            })
            
        except Exception as e:
            return self._error(f"Failed to disable auto-selection: {str(e)}")

    def _show_model_status(self) -> Dict[str, Any]:
        """Show comprehensive model status"""
        try:
            current_model = self.model_switcher.get_current_model()
            health_status = self.model_switcher.get_model_health(current_model.name)
            performance = self.model_switcher.get_model_performance(current_model.name)
            
            # Status indicators
            health_icon = "🟢" if health_status.get('status') == 'healthy' else "🟡" if health_status.get('status') == 'warning' else "🔴"
            auto_icon = "🔄" if self.model_switcher.is_auto_selection_enabled() else "🎯"
            
            status_text = (
                f"{health_icon} **Model Health**: {health_status.get('status', 'unknown').title()}\n"
                f"🤖 **Active Model**: {current_model.name} ({current_model.provider})\n"
                f"{auto_icon} **Selection Mode**: {'Auto' if self.model_switcher.is_auto_selection_enabled() else 'Manual'}\n\n"
                f"📊 **Performance (Last 24h)**:\n"
                f"• Requests: {performance.get('total_requests', 0):,}\n"
                f"• Success Rate: {performance.get('success_rate', 0):.1f}%\n"
                f"• Avg Response: {performance.get('avg_response_time', 0):.2f}s\n\n"
                f"💰 **Cost Information**:\n"
                f"• Current Session: ${self.cost_tracker.get_current_session_cost():.6f}\n"
                f"• Model Rate: ${current_model.cost_per_1k_tokens:.4f}/1K tokens\n\n"
                f"⚙️ **Configuration**:\n"
                f"• Max Tokens: {current_model.max_tokens:,}\n"
                f"• Context Window: {current_model.context_window:,}\n"
                f"• Rate Limit: {current_model.rate_limit} req/min"
            )
            
            if health_status.get('last_error'):
                status_text += f"\n\n⚠️ **Last Error**: {health_status['last_error']}"
            
            style = "green" if health_status.get('status') == 'healthy' else "yellow" if health_status.get('status') == 'warning' else "red"
            
            content = Panel(
                Text(status_text, style=style),
                title="🤖 Model Status",
                border_style=style
            )
            
            return self._success({
                'content': content,
                'health_status': health_status,
                'performance': performance,
                'current_model': current_model.name
            })
            
        except Exception as e:
            return self._error(f"Failed to show model status: {str(e)}")

    def _show_performance_analytics(self, days: int) -> Dict[str, Any]:
        """Show detailed performance analytics"""
        try:
            analytics = self.model_switcher.get_performance_analytics(days)
            
            if not analytics.get('models'):
                no_data = Panel(
                    Text(f"No performance data available for the last {days} days.", style="yellow"),
                    title="📊 Performance Analytics",
                    border_style="yellow"
                )
                return self._success({'content': no_data, 'analytics': analytics})
            
            # Performance table
            perf_table = Table(
                title=f"📊 Performance Analytics ({days} days)",
                show_header=True,
                header_style="bold cyan"
            )
            perf_table.add_column("Model", style="white", width=18)
            perf_table.add_column("Requests", style="yellow", width=10)
            perf_table.add_column("Success Rate", style="green", width=12)
            perf_table.add_column("Avg Response", style="blue", width=12)
            perf_table.add_column("Total Cost", style="red", width=12)
            perf_table.add_column("Reliability", style="cyan", width=12)
            
            for model_name, stats in analytics['models'].items():
                perf_table.add_row(
                    model_name,
                    f"{stats.get('requests', 0):,}",
                    f"{stats.get('success_rate', 0):.1f}%",
                    f"{stats.get('avg_response_time', 0):.2f}s",
                    f"${stats.get('total_cost', 0):.4f}",
                    f"{stats.get('reliability_score', 0):.1f}/10"
                )
            
            # Summary statistics
            summary_text = (
                f"📈 **Period Summary**\n\n"
                f"• Total Requests: {analytics.get('total_requests', 0):,}\n"
                f"• Overall Success Rate: {analytics.get('overall_success_rate', 0):.1f}%\n"
                f"• Average Response Time: {analytics.get('avg_response_time', 0):.2f}s\n"
                f"• Total Cost: ${analytics.get('total_cost', 0):.4f}\n"
                f"• Most Used Model: {analytics.get('most_used_model', 'N/A')}\n"
                f"• Best Performing: {analytics.get('best_performance_model', 'N/A')}\n"
                f"• Most Cost Effective: {analytics.get('most_cost_effective', 'N/A')}"
            )
            
            summary_panel = Panel(
                Text(summary_text, style="blue"),
                title="📈 Summary",
                border_style="blue"
            )
            
            content = Group(perf_table, "", summary_panel)
            
            return self._success({
                'content': content,
                'analytics': analytics,
                'period_days': days
            })
            
        except Exception as e:
            return self._error(f"Failed to show performance analytics: {str(e)}")

    def _compare_models(self, model1: str, model2: str) -> Dict[str, Any]:
        """Compare two models side by side"""
        try:
            comparison = self.model_switcher.compare_models(model1, model2)
            
            if 'error' in comparison:
                return self._error(comparison['error'])
            
            # Comparison table
            comp_table = Table(
                title=f"⚔️ Model Comparison: {model1} vs {model2}",
                show_header=True,
                header_style="bold cyan"
            )
            comp_table.add_column("Metric", style="white", width=20)
            comp_table.add_column(model1, style="green", width=20)
            comp_table.add_column(model2, style="blue", width=20)
            comp_table.add_column("Winner", style="yellow", width=15)
            
            metrics = [
                ("Provider", "provider", str),
                ("Avg Response Time", "avg_response_time", lambda x: f"{x:.2f}s"),
                ("Success Rate", "success_rate", lambda x: f"{x:.1f}%"),
                ("Cost per 1K tokens", "cost_per_1k_tokens", lambda x: f"${x:.4f}"),
                ("Max Tokens", "max_tokens", lambda x: f"{x:,}"),
                ("Context Window", "context_window", lambda x: f"{x:,}"),
                ("Rate Limit", "rate_limit", lambda x: f"{x} req/min"),
                ("Total Requests", "total_requests", lambda x: f"{x:,}")
            ]
            
            for metric_name, key, formatter in metrics:
                val1 = comparison['model1_stats'].get(key, 0)
                val2 = comparison['model2_stats'].get(key, 0)
                
                if isinstance(formatter, type):
                    formatted1 = str(val1) if val1 else "N/A"
                    formatted2 = str(val2) if val2 else "N/A"
                    winner = "Tie"
                else:
                    formatted1 = formatter(val1) if val1 else "N/A"
                    formatted2 = formatter(val2) if val2 else "N/A"
                    
                    # Determine winner (lower is better for response time and cost)
                    if key in ['avg_response_time', 'cost_per_1k_tokens']:
                        winner = model1 if val1 < val2 and val1 > 0 else model2 if val2 > 0 else "Tie"
                    else:
                        winner = model1 if val1 > val2 else model2 if val2 > val1 else "Tie"
                
                comp_table.add_row(metric_name, formatted1, formatted2, winner)
            
            # Overall recommendation
            recommendation = comparison.get('recommendation', 'No clear winner')
            rec_text = (
                f"🎯 **Overall Recommendation**: {recommendation}\n\n"
                f"📊 **Comparison Summary**:\n"
                f"• {model1} advantages: {', '.join(comparison.get('model1_advantages', []))}\n"
                f"• {model2} advantages: {', '.join(comparison.get('model2_advantages', []))}\n\n"
                f"💡 **Use Case Recommendations**:\n"
                f"• For cost optimization: {comparison.get('cost_winner', 'Tie')}\n"
                f"• For performance: {comparison.get('performance_winner', 'Tie')}\n"
                f"• For reliability: {comparison.get('reliability_winner', 'Tie')}"
            )
            
            rec_panel = Panel(
                Text(rec_text, style="cyan"),
                title="🎯 Recommendation",
                border_style="cyan"
            )
            
            content = Group(comp_table, "", rec_panel)
            
            return self._success({
                'content': content,
                'comparison': comparison,
                'models_compared': [model1, model2]
            })
            
        except Exception as e:
            return self._error(f"Failed to compare models: {str(e)}")

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
        logger.error(f"ModelCommand error: {message}")
        return {
            "success": False,
            "command": self.name,
            "error": message,
            "timestamp": datetime.now().isoformat()
        }


# Register the command
from deile.commands.registry import CommandRegistry
CommandRegistry.register("model", ModelCommand)