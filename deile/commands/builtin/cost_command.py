"""Cost Command - Display token usage and cost information"""

from typing import Dict, Any, Optional
import json
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text

from ..base import DirectCommand
from ...core.exceptions import CommandError


class CostCommand(DirectCommand):
    """Display token usage, cost estimation and run statistics"""
    
    def __init__(self):
        super().__init__(
            name="cost",
            description="Display token usage, cost estimation and run statistics.",
            aliases=["tokens", "usage"]
        )
    
    def execute(self, 
               args: str = "",
               context: Optional[Dict[str, Any]] = None) -> Any:
        """Execute cost command"""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            format_type = "summary"  # default
            export = False
            show_breakdown = False
            
            i = 0
            while i < len(parts):
                if parts[i] in ["--format", "-f"]:
                    if i + 1 < len(parts):
                        format_type = parts[i + 1]
                        i += 2
                    else:
                        raise CommandError("--format requires a value (summary, detailed, json)")
                elif parts[i] in ["--export", "-e"]:
                    export = True
                    i += 1
                elif parts[i] in ["--breakdown", "-b"]:
                    show_breakdown = True
                    i += 1
                else:
                    format_type = parts[i]  # Positional argument
                    i += 1
            
            if format_type not in ["summary", "detailed", "json"]:
                raise CommandError("Format must be one of: summary, detailed, json")
            
            # Get cost data from agent (this would be injected in real implementation)
            cost_data = self._get_cost_data(context)
            
            if format_type == "json":
                return json.dumps(cost_data, indent=2, default=str)
            
            # Create Rich display
            if format_type == "summary":
                return self._create_summary_display(cost_data, show_breakdown)
            else:  # detailed
                return self._create_detailed_display(cost_data, show_breakdown)
            
        except Exception as e:
            raise CommandError(f"Failed to display cost information: {str(e)}")
    
    def _get_cost_data(self, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Get cost data from agent (mock implementation)"""
        
        # In real implementation, this would get data from the agent
        return {
            "session": {
                "id": "session_20250906_184500",
                "started": "2025-09-06T15:30:00",
                "duration": "3h 15m",
                "total_requests": 23,
                "active_model": "gemini-2.5-pro"
            },
            "token_usage": {
                "total_prompt_tokens": 12500,
                "total_completion_tokens": 3725,
                "total_tokens": 16225,
                "breakdown": {
                    "system_instructions": 625,
                    "persona": 200,
                    "memory": 4700,
                    "conversation": 8500,
                    "tools": 2400
                }
            },
            "tool_calls": {
                "total_calls": 47,
                "successful": 44,
                "failed": 3,
                "by_tool": {
                    "bash_execute": {"calls": 15, "tokens": 2500},
                    "read_file": {"calls": 12, "tokens": 1800},
                    "write_file": {"calls": 8, "tokens": 1200},
                    "list_files": {"calls": 7, "tokens": 900},
                    "find_in_files": {"calls": 5, "tokens": 1100}
                }
            },
            "cost_estimation": {
                "model": "gemini-2.5-pro",
                "rate_per_1k_prompt": 0.00125,  # $0.00125 per 1K prompt tokens
                "rate_per_1k_completion": 0.005,  # $0.005 per 1K completion tokens
                "estimated_prompt_cost": 0.0156,  # 12.5K * $0.00125
                "estimated_completion_cost": 0.0186,  # 3.7K * $0.005
                "estimated_total_cost": 0.0342,
                "currency": "USD"
            },
            "efficiency_metrics": {
                "avg_tokens_per_request": 705,
                "avg_response_tokens": 162,
                "tool_success_rate": 93.6,
                "context_efficiency": 79.2  # percentage of context used
            }
        }
    
    def _create_summary_display(self, data: Dict[str, Any], show_breakdown: bool) -> Panel:
        """Create summary display"""
        
        token_data = data.get("token_usage", {})
        cost_data = data.get("cost_estimation", {})
        session_data = data.get("session", {})
        tools_data = data.get("tool_calls", {})
        
        content_lines = [
            f"💰 **Cost & Usage Summary**",
            "",
            f"⏱️  **Session**: {session_data.get('duration', 'Unknown')}",
            f"🤖 **Model**: {session_data.get('active_model', 'Unknown')}",
            f"📊 **Requests**: {session_data.get('total_requests', 0)}",
            f"🔧 **Tool Calls**: {tools_data.get('successful', 0)}/{tools_data.get('total_calls', 0)} successful",
            "",
            f"🎯 **Tokens**: {token_data.get('total_tokens', 0):,} total",
            f"   • Prompt: {token_data.get('total_prompt_tokens', 0):,}",
            f"   • Completion: {token_data.get('total_completion_tokens', 0):,}",
            "",
            f"💵 **Estimated Cost**: ${cost_data.get('estimated_total_cost', 0):.4f}",
            f"   • Prompt: ${cost_data.get('estimated_prompt_cost', 0):.4f}",
            f"   • Completion: ${cost_data.get('estimated_completion_cost', 0):.4f}"
        ]
        
        if show_breakdown:
            breakdown = token_data.get("breakdown", {})
            content_lines.extend([
                "",
                "📋 **Token Breakdown**:",
                f"   • System: {breakdown.get('system_instructions', 0):,}",
                f"   • Persona: {breakdown.get('persona', 0):,}",
                f"   • Memory: {breakdown.get('memory', 0):,}",
                f"   • Conversation: {breakdown.get('conversation', 0):,}",
                f"   • Tools: {breakdown.get('tools', 0):,}"
            ])
        
        content = "\n".join(content_lines)
        
        return Panel(
            Text(content, style="white"),
            title="💰 Cost Analysis",
            border_style="green",
            padding=(1, 2)
        )
    
    def _create_detailed_display(self, data: Dict[str, Any], show_breakdown: bool) -> Columns:
        """Create detailed display with multiple panels"""
        
        panels = []
        
        # Session & Model Panel
        session_data = data.get("session", {})
        cost_data = data.get("cost_estimation", {})
        
        session_content = [
            f"**Session ID**: {session_data.get('id', 'Unknown')}",
            f"**Started**: {session_data.get('started', 'Unknown')[:16]}",
            f"**Duration**: {session_data.get('duration', 'Unknown')}",
            f"**Requests**: {session_data.get('total_requests', 0)}",
            "",
            f"**Model**: {cost_data.get('model', 'Unknown')}",
            f"**Prompt Rate**: ${cost_data.get('rate_per_1k_prompt', 0):.5f}/1K",
            f"**Completion Rate**: ${cost_data.get('rate_per_1k_completion', 0):.5f}/1K"
        ]
        
        panels.append(Panel(
            "\n".join(session_content),
            title="📊 Session Info",
            border_style="blue"
        ))
        
        # Token Usage Panel
        token_data = data.get("token_usage", {})
        
        token_content = [
            f"**Total Tokens**: {token_data.get('total_tokens', 0):,}",
            f"**Prompt Tokens**: {token_data.get('total_prompt_tokens', 0):,}",
            f"**Completion Tokens**: {token_data.get('total_completion_tokens', 0):,}",
            "",
            f"**Estimated Cost**: ${cost_data.get('estimated_total_cost', 0):.4f}",
            f"  Prompt: ${cost_data.get('estimated_prompt_cost', 0):.4f}",
            f"  Completion: ${cost_data.get('estimated_completion_cost', 0):.4f}"
        ]
        
        panels.append(Panel(
            "\n".join(token_content),
            title="🎯 Token Usage",
            border_style="yellow"
        ))
        
        # Tool Usage Panel
        tools_data = data.get("tool_calls", {})
        by_tool = tools_data.get("by_tool", {})
        
        tool_content = [
            f"**Total Calls**: {tools_data.get('total_calls', 0)}",
            f"**Successful**: {tools_data.get('successful', 0)}",
            f"**Failed**: {tools_data.get('failed', 0)}",
            f"**Success Rate**: {data.get('efficiency_metrics', {}).get('tool_success_rate', 0):.1f}%",
            "",
            "**Top Tools**:"
        ]
        
        # Add top 3 tools by usage
        sorted_tools = sorted(by_tool.items(), 
                            key=lambda x: x[1].get('calls', 0), 
                            reverse=True)[:3]
        
        for tool_name, tool_data in sorted_tools:
            tool_content.append(f"  {tool_name}: {tool_data.get('calls', 0)} calls")
        
        panels.append(Panel(
            "\n".join(tool_content),
            title="🔧 Tool Usage",
            border_style="cyan"
        ))
        
        if show_breakdown:
            # Detailed Breakdown Panel
            breakdown = token_data.get("breakdown", {})
            efficiency = data.get("efficiency_metrics", {})
            
            breakdown_content = [
                "**Token Distribution**:",
                f"  System: {breakdown.get('system_instructions', 0):,}",
                f"  Persona: {breakdown.get('persona', 0):,}",
                f"  Memory: {breakdown.get('memory', 0):,}",
                f"  Conversation: {breakdown.get('conversation', 0):,}",
                f"  Tools: {breakdown.get('tools', 0):,}",
                "",
                "**Efficiency Metrics**:",
                f"  Avg tokens/req: {efficiency.get('avg_tokens_per_request', 0)}",
                f"  Avg response: {efficiency.get('avg_response_tokens', 0)}",
                f"  Context usage: {efficiency.get('context_efficiency', 0):.1f}%"
            ]
            
            panels.append(Panel(
                "\n".join(breakdown_content),
                title="📋 Detailed Breakdown",
                border_style="red"
            ))
        
        return Columns(panels, equal=True, expand=True)
    
    def get_help(self) -> str:
        """Get command help"""
        return """Display token usage, cost estimation and run statistics

Usage:
  /cost [format] [options]

Formats:
  summary   Show summary view (default)
  detailed  Show detailed breakdown
  json      Export as JSON

Options:
  --breakdown, -b      Show detailed token breakdown
  --export, -e         Export cost data to file
  --format FORMAT, -f  Specify output format

Examples:
  /cost                    Show cost summary
  /cost detailed -b        Show detailed view with breakdown
  /cost json               Export as JSON
  /cost --breakdown        Show summary with token breakdown

Aliases: /tokens, /usage"""