"""Tools Command - Display available tools and their schemas"""

from typing import Dict, Any, Optional
import json
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.syntax import Syntax

from ..base import DirectCommand
from ...core.exceptions import CommandError


class ToolsCommand(DirectCommand):
    """Display available tools, their schemas and usage statistics"""
    
    def __init__(self):
        super().__init__(
            name="tools",
            description="Display available tools, their schemas and usage statistics.",
            aliases=["tool", "schemas"]
        )
    
    def execute(self, 
               args: str = "",
               context: Optional[Dict[str, Any]] = None) -> Any:
        """Execute tools command"""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            format_type = "list"  # default
            tool_name = None
            show_schema = False
            show_examples = False
            
            i = 0
            while i < len(parts):
                if parts[i] in ["--format", "-f"]:
                    if i + 1 < len(parts):
                        format_type = parts[i + 1]
                        i += 2
                    else:
                        raise CommandError("--format requires a value (list, detailed, json)")
                elif parts[i] in ["--schema", "-s"]:
                    show_schema = True
                    i += 1
                elif parts[i] in ["--examples", "-e"]:
                    show_examples = True
                    i += 1
                elif parts[i].startswith("--"):
                    raise CommandError(f"Unknown option: {parts[i]}")
                else:
                    # Positional argument - either format or tool name
                    if format_type == "list" and parts[i] in ["list", "detailed", "json"]:
                        format_type = parts[i]
                    else:
                        tool_name = parts[i]
                    i += 1
            
            if format_type not in ["list", "detailed", "json"]:
                raise CommandError("Format must be one of: list, detailed, json")
            
            # Get tools data from registry (this would be injected in real implementation)
            tools_data = self._get_tools_data(context, tool_name)
            
            if format_type == "json":
                return json.dumps(tools_data, indent=2, default=str)
            
            # Single tool display
            if tool_name:
                return self._create_single_tool_display(
                    tools_data.get("tool", {}), show_schema, show_examples
                )
            
            # Create Rich display for all tools
            if format_type == "list":
                return self._create_list_display(tools_data)
            else:  # detailed
                return self._create_detailed_display(tools_data, show_schema, show_examples)
            
        except Exception as e:
            raise CommandError(f"Failed to display tools information: {str(e)}")
    
    def _get_tools_data(self, context: Optional[Dict[str, Any]], tool_name: Optional[str]) -> Dict[str, Any]:
        """Get tools data from registry (mock implementation)"""
        
        # Mock tools data - in real implementation this would come from ToolRegistry
        all_tools = {
            "bash_execute": {
                "name": "bash_execute",
                "description": "Execute bash commands with PTY support and security controls",
                "category": "execution",
                "risk_level": "variable",
                "display_policy": "system",
                "parameters": {
                    "command": {"type": "string", "required": True},
                    "working_directory": {"type": "string", "required": False},
                    "timeout": {"type": "number", "default": 60},
                    "use_pty": {"type": "boolean", "default": False},
                    "show_cli": {"type": "boolean", "default": True}
                },
                "examples": [
                    {"command": "ls -la", "description": "List files with details"},
                    {"command": "python script.py", "description": "Run Python script"}
                ],
                "usage_stats": {"total_calls": 15, "success_rate": 93.3, "avg_duration": 2.4}
            },
            "read_file": {
                "name": "read_file",
                "description": "Read contents of a file with encoding detection",
                "category": "file",
                "risk_level": "safe",
                "display_policy": "agent",
                "parameters": {
                    "path": {"type": "string", "required": True},
                    "encoding": {"type": "string", "default": "auto"},
                    "max_size": {"type": "number", "default": 1048576}
                },
                "examples": [
                    {"path": "./config.yaml", "description": "Read configuration file"},
                    {"path": "logs/app.log", "encoding": "utf-8", "description": "Read log file"}
                ],
                "usage_stats": {"total_calls": 12, "success_rate": 100.0, "avg_duration": 0.1}
            },
            "write_file": {
                "name": "write_file", 
                "description": "Write content to a file with backup and validation",
                "category": "file",
                "risk_level": "moderate", 
                "display_policy": "system",
                "parameters": {
                    "path": {"type": "string", "required": True},
                    "content": {"type": "string", "required": True},
                    "encoding": {"type": "string", "default": "utf-8"},
                    "create_backup": {"type": "boolean", "default": True}
                },
                "examples": [
                    {"path": "new_file.txt", "content": "Hello world", "description": "Create new file"},
                    {"path": "config.json", "content": "{}", "description": "Update config file"}
                ],
                "usage_stats": {"total_calls": 8, "success_rate": 87.5, "avg_duration": 0.3}
            },
            "list_files": {
                "name": "list_files",
                "description": "List files and directories with filtering options",
                "category": "file", 
                "risk_level": "safe",
                "display_policy": "system",
                "parameters": {
                    "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                    "show_hidden": {"type": "boolean", "default": False},
                    "pattern": {"type": "string", "required": False}
                },
                "examples": [
                    {"path": ".", "recursive": True, "description": "List all files recursively"},
                    {"pattern": "*.py", "description": "List Python files only"}
                ],
                "usage_stats": {"total_calls": 7, "success_rate": 100.0, "avg_duration": 0.2}
            },
            "find_in_files": {
                "name": "find_in_files",
                "description": "Search for text patterns in files with context limits",
                "category": "search",
                "risk_level": "safe", 
                "display_policy": "system",
                "parameters": {
                    "pattern": {"type": "string", "required": True},
                    "path": {"type": "string", "default": "."},
                    "regex": {"type": "boolean", "default": False},
                    "max_context_lines": {"type": "number", "default": 5, "max": 50}
                },
                "examples": [
                    {"pattern": "TODO", "description": "Find TODO comments"},
                    {"pattern": "class \\w+", "regex": True, "description": "Find class definitions"}
                ],
                "usage_stats": {"total_calls": 5, "success_rate": 100.0, "avg_duration": 1.8}
            }
        }
        
        if tool_name:
            if tool_name in all_tools:
                return {"tool": all_tools[tool_name]}
            else:
                raise CommandError(f"Tool '{tool_name}' not found")
        
        return {
            "tools": all_tools,
            "summary": {
                "total_tools": len(all_tools),
                "by_category": {
                    "file": 3,
                    "execution": 1,
                    "search": 1
                },
                "by_risk": {
                    "safe": 3,
                    "moderate": 1,
                    "variable": 1
                }
            }
        }
    
    def _create_single_tool_display(self, tool_data: Dict[str, Any], 
                                  show_schema: bool, show_examples: bool) -> Panel:
        """Create display for a single tool"""
        
        content_lines = [
            f"**{tool_data.get('name', 'Unknown')}**",
            "",
            f"📝 **Description**: {tool_data.get('description', 'No description')}",
            f"📂 **Category**: {tool_data.get('category', 'unknown')}",
            f"⚠️  **Risk Level**: {tool_data.get('risk_level', 'unknown')}",
            f"📺 **Display Policy**: {tool_data.get('display_policy', 'unknown')}",
            ""
        ]
        
        # Usage stats
        stats = tool_data.get("usage_stats", {})
        if stats:
            content_lines.extend([
                "📊 **Usage Statistics**:",
                f"  • Total Calls: {stats.get('total_calls', 0)}",
                f"  • Success Rate: {stats.get('success_rate', 0):.1f}%",
                f"  • Avg Duration: {stats.get('avg_duration', 0):.1f}s",
                ""
            ])
        
        # Parameters
        params = tool_data.get("parameters", {})
        if params:
            content_lines.extend([
                "⚙️  **Parameters**:"
            ])
            for param_name, param_info in params.items():
                required = " (required)" if param_info.get("required") else ""
                default = f" [default: {param_info.get('default')}]" if "default" in param_info else ""
                content_lines.append(f"  • **{param_name}**: {param_info.get('type', 'unknown')}{required}{default}")
            content_lines.append("")
        
        # Examples
        examples = tool_data.get("examples", [])
        if examples and show_examples:
            content_lines.extend([
                "💡 **Examples**:"
            ])
            for i, example in enumerate(examples[:3], 1):  # Show max 3 examples
                desc = example.get("description", f"Example {i}")
                content_lines.append(f"  {i}. {desc}")
                # Show first parameter as example
                first_param = next(iter(example.keys() - {"description"}), None)
                if first_param:
                    content_lines.append(f"     {first_param}: {example[first_param]}")
            content_lines.append("")
        
        # Schema
        if show_schema and params:
            schema_json = json.dumps(params, indent=2)
            content_lines.extend([
                "🔧 **JSON Schema**:",
                "```json",
                schema_json,
                "```"
            ])
        
        content = "\n".join(content_lines)
        
        return Panel(
            Text(content, style="white"),
            title=f"🔧 {tool_data.get('name', 'Tool')}",
            border_style="cyan",
            padding=(1, 2)
        )
    
    def _create_list_display(self, data: Dict[str, Any]) -> Table:
        """Create list display for all tools"""
        
        table = Table(title="🔧 Available Tools", show_header=True, header_style="bold magenta")
        table.add_column("Tool Name", style="cyan", width=15)
        table.add_column("Category", style="green", width=10) 
        table.add_column("Risk", style="yellow", width=8)
        table.add_column("Calls", justify="right", style="blue", width=6)
        table.add_column("Success%", justify="right", style="green", width=8)
        table.add_column("Description", style="white", width=40)
        
        tools = data.get("tools", {})
        for tool_name, tool_data in sorted(tools.items()):
            stats = tool_data.get("usage_stats", {})
            
            table.add_row(
                tool_name,
                tool_data.get("category", "unknown"),
                tool_data.get("risk_level", "unknown"),
                str(stats.get("total_calls", 0)),
                f"{stats.get('success_rate', 0):.1f}",
                tool_data.get("description", "No description")[:40] + ("..." if len(tool_data.get("description", "")) > 40 else "")
            )
        
        return table
    
    def _create_detailed_display(self, data: Dict[str, Any], 
                               show_schema: bool, show_examples: bool) -> Columns:
        """Create detailed display with multiple panels"""
        
        panels = []
        summary = data.get("summary", {})
        
        # Summary Panel
        summary_content = [
            f"**Total Tools**: {summary.get('total_tools', 0)}",
            "",
            "**By Category**:"
        ]
        
        by_category = summary.get("by_category", {})
        for category, count in sorted(by_category.items()):
            summary_content.append(f"  • {category}: {count}")
        
        summary_content.extend([
            "",
            "**By Risk Level**:"
        ])
        
        by_risk = summary.get("by_risk", {})
        for risk, count in sorted(by_risk.items()):
            summary_content.append(f"  • {risk}: {count}")
        
        panels.append(Panel(
            "\n".join(summary_content),
            title="📊 Summary",
            border_style="blue"
        ))
        
        # Tool details - show top 3 most used tools
        tools = data.get("tools", {})
        sorted_tools = sorted(tools.items(), 
                            key=lambda x: x[1].get("usage_stats", {}).get("total_calls", 0), 
                            reverse=True)
        
        for i, (tool_name, tool_data) in enumerate(sorted_tools[:3]):
            stats = tool_data.get("usage_stats", {})
            
            tool_content = [
                f"**{tool_name}**",
                "",
                f"📝 {tool_data.get('description', 'No description')[:50]}{'...' if len(tool_data.get('description', '')) > 50 else ''}",
                "",
                f"📂 Category: {tool_data.get('category', 'unknown')}",
                f"⚠️  Risk: {tool_data.get('risk_level', 'unknown')}",
                f"📺 Display: {tool_data.get('display_policy', 'unknown')}",
                "",
                f"📊 Calls: {stats.get('total_calls', 0)}",
                f"✅ Success: {stats.get('success_rate', 0):.1f}%",
                f"⏱️  Avg: {stats.get('avg_duration', 0):.1f}s"
            ]
            
            border_colors = ["green", "yellow", "cyan"]
            panels.append(Panel(
                "\n".join(tool_content),
                title=f"🔧 {tool_name}",
                border_style=border_colors[i]
            ))
        
        return Columns(panels, equal=True, expand=True)
    
    def get_help(self) -> str:
        """Get command help"""
        return """Display available tools, their schemas and usage statistics

Usage:
  /tools [tool_name] [format] [options]

Arguments:
  tool_name         Show details for specific tool

Formats:
  list              Show table of all tools (default)
  detailed          Show detailed panels
  json              Export as JSON

Options:
  --schema, -s      Show JSON schema for parameters
  --examples, -e    Show usage examples
  --format FORMAT, -f  Specify output format

Examples:
  /tools                     List all tools
  /tools bash_execute        Show details for bash_execute tool
  /tools detailed --schema   Show detailed view with schemas
  /tools read_file -s -e     Show read_file with schema and examples

Aliases: /tool, /schemas"""