"""Tools Command - Display available tools and their schemas"""

from __future__ import annotations

import json
from typing import Any

from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import (ArgSpec, parse_flag_args, promote_positional_format,
                      split_args, truncate)


class ToolsCommand(DirectCommand):
    """Display available tools, their schemas and usage statistics"""

    cli_flag = "--tools"
    cli_help = "List registered tools and exit."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        super().__init__(CommandConfig(
            name="tools",
            description="Display available tools, their schemas and usage statistics.",
        ))

    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute the tools command.

        Reads :class:`CommandContext.args` (the registry contract). Returns a
        :class:`CommandResult` whose ``content`` is a Rich renderable or a
        JSON string depending on the requested ``--format``.
        """
        try:
            parts = split_args(context)
            flags, positionals = parse_flag_args(
                parts,
                [
                    ArgSpec(("--format", "-f"), takes_value=True, dest="format"),
                    ArgSpec(("--schema", "-s"), dest="show_schema"),
                    ArgSpec(("--examples", "-e"), dest="show_examples"),
                ],
                strict=True,
            )
            format_type = flags.get("format", "list")
            show_schema = bool(flags.get("show_schema"))
            show_examples = bool(flags.get("show_examples"))
            tool_name = None
            # Positional: first {list,detailed,json} promotes to format (if still default);
            # any other positional is treated as tool name (last wins, matching prior behaviour).
            format_type, leftover_positionals = promote_positional_format(
                positionals, format_type, "list", ("list", "detailed", "json"),
            )
            for token in leftover_positionals:
                tool_name = token

            if format_type not in ["list", "detailed", "json"]:
                raise CommandError("Format must be one of: list, detailed, json")

            # Get tools data from registry (this would be injected in real implementation)
            tools_data = self._get_tools_data(context, tool_name)

            if format_type == "json":
                return CommandResult.success_result(
                    json.dumps(tools_data, indent=2, default=str), "text"
                )

            # Single tool display
            if tool_name:
                return CommandResult.success_result(
                    self._create_single_tool_display(
                        tools_data.get("tool", {}), show_schema, show_examples
                    ),
                    "rich",
                )

            # Create Rich display for all tools
            if format_type == "list":
                return CommandResult.success_result(
                    self._create_list_display(tools_data), "rich"
                )
            else:  # detailed
                return CommandResult.success_result(
                    self._create_detailed_display(tools_data, show_schema, show_examples),
                    "rich",
                )

        except Exception as e:
            return CommandResult.error_result(
                f"Failed to display tools information: {str(e)}", error=e
            )
    
    def _get_tools_data(self, context: CommandContext | None, tool_name: str | None) -> dict[str, Any]:
        """Read tools data from the live :class:`ToolRegistry`.

        Walks every registered tool, extracts schema (parameters, security
        level, category) and runtime stats (execution_count, enabled). No
        mock fallback — if the registry is empty, we return an empty set so
        ``--tools`` honestly reflects the runtime state.
        """
        from collections import Counter

        from ...tools.registry import get_tool_registry

        registry = get_tool_registry()
        if len(registry) == 0:
            registry.auto_discover()

        tools: dict[str, dict[str, Any]] = {}
        for tool in registry.list_all():
            tools[tool.name] = self._serialize_tool(tool)

        if tool_name:
            if tool_name not in tools:
                raise CommandError(f"Tool '{tool_name}' not found")
            return {"tool": tools[tool_name]}

        by_category = Counter(t["category"] for t in tools.values())
        by_risk = Counter(t["risk_level"] for t in tools.values())
        return {
            "tools": tools,
            "summary": {
                "total_tools": len(tools),
                "by_category": dict(by_category),
                "by_risk": dict(by_risk),
            },
        }

    @staticmethod
    def _serialize_tool(tool: Any) -> dict[str, Any]:
        """Project a :class:`Tool` to the dict shape used by the renderers."""
        schema = getattr(tool, "schema", None)
        params: dict[str, Any] = {}
        if schema is not None:
            required = set(schema.required or [])
            for pname, pspec in (schema.parameters or {}).items():
                if isinstance(pspec, dict):
                    entry: dict[str, Any] = {
                        "type": pspec.get("type", "any"),
                        "required": pname in required,
                    }
                    if "default" in pspec:
                        entry["default"] = pspec["default"]
                    params[pname] = entry
                else:
                    params[pname] = {"type": "any", "required": pname in required}
        risk = getattr(getattr(schema, "security_level", None), "value", "unknown") if schema else "unknown"
        category = getattr(getattr(schema, "category", None), "value", None) or getattr(tool, "category", "unknown")
        return {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "category": str(category),
            "risk_level": str(risk),
            "display_policy": "system",
            "parameters": params,
            "examples": [],
            "usage_stats": {
                "total_calls": int(getattr(tool, "execution_count", 0) or 0),
                "success_rate": 0.0,
                "avg_duration": 0.0,
            },
        }
    
    def _create_single_tool_display(self, tool_data: dict[str, Any], 
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
    
    def _create_list_display(self, data: dict[str, Any]) -> Table:
        """Create list display for all tools"""
        
        table = Table(title="🔧 Available Tools", show_header=True, header_style="bold magenta")
        table.add_column("Tool Name", style="cyan")
        table.add_column("Category", style="green") 
        table.add_column("Risk", style="yellow")
        table.add_column("Calls", justify="right", style="blue")
        table.add_column("Success%", justify="right", style="green")
        table.add_column("Description", style="white")
        
        tools = data.get("tools", {})
        for tool_name, tool_data in sorted(tools.items()):
            stats = tool_data.get("usage_stats", {})
            
            table.add_row(
                tool_name,
                tool_data.get("category", "unknown"),
                tool_data.get("risk_level", "unknown"),
                str(stats.get("total_calls", 0)),
                f"{stats.get('success_rate', 0):.1f}",
                truncate(tool_data.get("description", "No description"), 40),
            )
        
        return table
    
    def _create_detailed_display(self, data: dict[str, Any], 
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
  /tools read_file -s -e     Show read_file with schema and examples"""