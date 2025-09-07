"""Context Command - Display LLM context information"""

from typing import Dict, Any, Optional
import json
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text

from ..base import DirectCommand, CommandResult
from ...core.exceptions import CommandError


class ContextCommand(DirectCommand):
    """Display complete LLM context: system instructions, memory, history, tools and token usage breakdown"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="context",
            description="Display complete LLM context: system instructions, memory, history, tools and token usage breakdown.",
            aliases=["ctx", "info"]
        )
        super().__init__(config)
    
    async def execute(self, context) -> Any:
        """Execute context command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            format_type = "summary"  # default
            export = False
            show_tokens = False
            
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
                elif parts[i] in ["--show-tokens", "-t"]:
                    show_tokens = True
                    i += 1
                else:
                    format_type = parts[i]  # Positional argument
                    i += 1
            
            if format_type not in ["summary", "detailed", "json"]:
                raise CommandError("Format must be one of: summary, detailed, json")
            
            # Get context from agent (this would be injected in real implementation)
            context_data = self._get_context_data(context)
            
            if format_type == "json":
                return json.dumps(context_data, indent=2, default=str)
            
            # Create Rich display
            if format_type == "summary":
                return self._create_summary_display(context_data, show_tokens)
            else:  # detailed
                return self._create_detailed_display(context_data, show_tokens)
            
        except Exception as e:
            raise CommandError(f"Failed to display context: {str(e)}")
    
    def _get_context_data(self, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Get context data from agent (mock implementation)"""
        
        # In real implementation, this would get data from the agent
        return {
            "system_instructions": {
                "length": 2500,
                "tokens": 625,
                "content_preview": "You are DEILE, an AI assistant specialized in software development..."
            },
            "persona": {
                "active": True,
                "length": 800,
                "tokens": 200,
                "name": "Developer Assistant"
            },
            "memory": {
                "short_term": {
                    "entries": 15,
                    "tokens": 1200,
                    "last_update": "2025-09-06T18:45:00"
                },
                "long_term": {
                    "entries": 45,
                    "tokens": 3500,
                    "indexed": True
                }
            },
            "conversation_history": {
                "messages": 23,
                "tokens": 8500,
                "oldest_message": "2025-09-06T15:30:00",
                "newest_message": "2025-09-06T18:44:30"
            },
            "tools": {
                "total": 12,
                "enabled": 11,
                "categories": ["file", "execution", "search", "network"],
                "total_tokens": 2400
            },
            "model": {
                "name": "gemini-2.5-pro",
                "max_tokens": 2048000,
                "temperature": 0.7,
                "provider": "Google GenAI"
            },
            "token_usage": {
                "total": 16225,
                "percentage": 0.79,
                "breakdown": {
                    "system": 625,
                    "persona": 200,
                    "memory": 4700,
                    "history": 8500,
                    "tools": 2400
                }
            },
            "session": {
                "id": "session_20250906_184500",
                "started": "2025-09-06T15:30:00",
                "duration": "3h 15m",
                "requests": 23
            }
        }
    
    def _create_summary_display(self, data: Dict[str, Any], show_tokens: bool) -> Panel:
        """Create summary display"""
        
        # Token usage summary
        token_data = data.get("token_usage", {})
        total_tokens = token_data.get("total", 0)
        percentage = token_data.get("percentage", 0) * 100
        
        # Create content
        content_lines = [
            f"📊 **Context Overview**",
            "",
            f"🤖 **Model**: {data.get('model', {}).get('name', 'Unknown')}",
            f"⏱️  **Session**: {data.get('session', {}).get('duration', 'Unknown')}",
            f"💬 **Messages**: {data.get('conversation_history', {}).get('messages', 0)}",
            f"🔧 **Tools**: {data.get('tools', {}).get('enabled', 0)}/{data.get('tools', {}).get('total', 0)} enabled"
        ]
        
        if show_tokens:
            content_lines.extend([
                "",
                f"🎯 **Token Usage**: {total_tokens:,} ({percentage:.1f}%)",
                f"   • System: {token_data.get('breakdown', {}).get('system', 0):,}",
                f"   • Memory: {token_data.get('breakdown', {}).get('memory', 0):,}",
                f"   • History: {token_data.get('breakdown', {}).get('history', 0):,}",
                f"   • Tools: {token_data.get('breakdown', {}).get('tools', 0):,}"
            ])
        
        content = "\n".join(content_lines)
        
        return Panel(
            Text(content, style="white"),
            title="🧠 LLM Context",
            border_style="blue",
            padding=(1, 2)
        )
    
    def _create_detailed_display(self, data: Dict[str, Any], show_tokens: bool) -> Columns:
        """Create detailed display with multiple panels"""
        
        panels = []
        
        # System & Model Panel
        model_info = data.get("model", {})
        system_info = data.get("system_instructions", {})
        
        model_content = [
            f"**Model**: {model_info.get('name', 'Unknown')}",
            f"**Provider**: {model_info.get('provider', 'Unknown')}",
            f"**Max Tokens**: {model_info.get('max_tokens', 0):,}",
            f"**Temperature**: {model_info.get('temperature', 0.7)}",
            "",
            f"**System Instructions**:",
            f"  Length: {system_info.get('length', 0)} chars",
            f"  Tokens: {system_info.get('tokens', 0):,}"
        ]
        
        panels.append(Panel(
            "\n".join(model_content),
            title="🤖 Model & System",
            border_style="green"
        ))
        
        # Memory Panel
        memory = data.get("memory", {})
        short_term = memory.get("short_term", {})
        long_term = memory.get("long_term", {})
        
        memory_content = [
            f"**Short-term Memory**:",
            f"  Entries: {short_term.get('entries', 0)}",
            f"  Tokens: {short_term.get('tokens', 0):,}",
            f"  Updated: {short_term.get('last_update', 'Never')[:16]}",
            "",
            f"**Long-term Memory**:",
            f"  Entries: {long_term.get('entries', 0)}",
            f"  Tokens: {long_term.get('tokens', 0):,}",
            f"  Indexed: {'Yes' if long_term.get('indexed') else 'No'}"
        ]
        
        panels.append(Panel(
            "\n".join(memory_content),
            title="🧠 Memory",
            border_style="yellow"
        ))
        
        # Tools Panel
        tools = data.get("tools", {})
        
        tools_content = [
            f"**Available Tools**: {tools.get('total', 0)}",
            f"**Enabled**: {tools.get('enabled', 0)}",
            f"**Categories**: {', '.join(tools.get('categories', []))}",
            f"**Schema Tokens**: {tools.get('total_tokens', 0):,}"
        ]
        
        panels.append(Panel(
            "\n".join(tools_content),
            title="🔧 Tools",
            border_style="cyan"
        ))
        
        if show_tokens:
            # Token Usage Panel
            token_data = data.get("token_usage", {})
            breakdown = token_data.get("breakdown", {})
            
            token_content = [
                f"**Total**: {token_data.get('total', 0):,} tokens",
                f"**Usage**: {token_data.get('percentage', 0) * 100:.1f}%",
                "",
                "**Breakdown**:",
                f"  System: {breakdown.get('system', 0):,}",
                f"  Persona: {breakdown.get('persona', 0):,}",
                f"  Memory: {breakdown.get('memory', 0):,}", 
                f"  History: {breakdown.get('history', 0):,}",
                f"  Tools: {breakdown.get('tools', 0):,}"
            ]
            
            panels.append(Panel(
                "\n".join(token_content),
                title="🎯 Token Usage",
                border_style="red"
            ))
        
        return Columns(panels, equal=True, expand=True)
    
    def get_help(self) -> str:
        """Get command help"""
        return """Display LLM context information

Usage:
  /context [format] [options]

Formats:
  summary   Show summary view (default)
  detailed  Show detailed breakdown
  json      Export as JSON

Options:
  --show-tokens, -t    Show detailed token usage
  --export, -e         Export context to file
  --format FORMAT, -f  Specify output format

Examples:
  /context                    Show summary
  /context detailed -t        Show detailed view with tokens
  /context json               Export as JSON
  /context --show-tokens      Show summary with token breakdown

Aliases: /ctx, /info"""