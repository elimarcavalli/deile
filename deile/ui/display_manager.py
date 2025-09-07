"""Enhanced Display Manager for DEILE - Solves SITUAÇÃO 1-3"""

from typing import Any, Dict, Optional, List
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from rich.progress import Progress
from rich.syntax import Syntax
from rich.text import Text
from rich.columns import Columns
import json
import logging

from ..tools.base import ToolResult, DisplayPolicy


logger = logging.getLogger(__name__)


class DisplayManager:
    """Enhanced display management with tool output formatting"""
    
    def __init__(self, console: Console):
        self.console = console
        
    def display_tool_result(self, 
                           tool_name: str,
                           result: ToolResult,
                           display_policy: Optional[str] = None) -> None:
        """Display tool result according to policy - SOLVES SITUAÇÃO 2 & 3"""
        
        # Use result's display policy or parameter override
        policy = DisplayPolicy(display_policy) if display_policy else result.display_policy
        
        # Only display if policy allows and show_cli is True
        if policy == DisplayPolicy.SILENT or not result.show_cli:
            return
        
        if policy in [DisplayPolicy.SYSTEM, DisplayPolicy.BOTH]:
            self._render_tool_output(tool_name, result)
            
    def _render_tool_output(self, tool_name: str, result: ToolResult) -> None:
        """Render tool output with appropriate formatting"""
        
        # Special handling for specific tools
        if tool_name == "list_files":
            self._display_list_files(result)
        elif tool_name == "find_in_files":
            self._display_search_results(result)
        else:
            self._display_generic_tool_result(tool_name, result)
            
    def _display_list_files(self, result: ToolResult) -> None:
        """Format file listing without broken characters - SOLVES SITUAÇÃO 1"""
        
        if not result.data:
            self.console.print("[yellow]No files found[/yellow]")
            return
        
        # Create proper tree structure
        tree = Tree("📁 Files", style="blue bold")
        
        files_data = result.data
        if isinstance(files_data, dict) and "files" in files_data:
            files = files_data["files"]
        elif isinstance(files_data, list):
            files = files_data
        else:
            # Fallback to generic display
            self._display_generic_tool_result("list_files", result)
            return
            
        # Group files by directory for better organization
        dirs = {}
        for file_info in files:
            if isinstance(file_info, dict):
                file_path = file_info.get("path", str(file_info))
                file_type = file_info.get("type", "file")
                file_size = file_info.get("size")
            else:
                file_path = str(file_info)
                file_type = "file"
                file_size = None
            
            # Split path components
            parts = file_path.replace('\\', '/').split('/')
            if len(parts) > 1:
                dir_name = '/'.join(parts[:-1])
                file_name = parts[-1]
            else:
                dir_name = "."
                file_name = file_path
            
            if dir_name not in dirs:
                dirs[dir_name] = []
            dirs[dir_name].append({
                "name": file_name,
                "type": file_type,
                "size": file_size
            })
        
        # Build tree with proper line breaks
        for dir_name, dir_files in sorted(dirs.items()):
            if dir_name == ".":
                parent_node = tree
            else:
                parent_node = tree.add(f"📁 {dir_name}/", style="blue")
            
            for file_info in sorted(dir_files, key=lambda x: x["name"]):
                icon = "📄" if file_info["type"] == "file" else "📁"
                name = file_info["name"]
                
                if file_info["size"] is not None:
                    size_str = self._format_file_size(file_info["size"])
                    parent_node.add(f"{icon} {name} [dim]({size_str})[/dim]")
                else:
                    parent_node.add(f"{icon} {name}")
        
        # Display with proper spacing
        self.console.print()
        self.console.print(tree)
        self.console.print()
        
    def _display_search_results(self, result: ToolResult) -> None:
        """Format search results with context highlighting"""
        
        if not result.data or not result.data.get("matches"):
            self.console.print("[yellow]No matches found[/yellow]")
            return
            
        matches = result.data["matches"]
        search_info = result.display_data or {}
        
        # Header with search summary
        summary = search_info.get("summary", f"{len(matches)} matches found")
        panel = Panel(
            Text(summary, style="green bold"),
            title="🔍 Search Results",
            border_style="green"
        )
        self.console.print(panel)
        
        # Display matches with context
        for i, match in enumerate(matches, 1):
            if i > 20:  # Limit display
                remaining = len(matches) - 20
                self.console.print(f"\n[dim]... and {remaining} more matches[/dim]")
                break
                
            # File header
            self.console.print(f"\n[blue bold]{match['file']}:{match['line_number']}[/blue bold]")
            
            # Context before
            for line in match.get("context_before", []):
                self.console.print(f"[dim]{line}[/dim]")
            
            # Matched line (highlighted)
            matched_line = match["match_text"]
            self.console.print(f"[yellow on black]{matched_line}[/yellow on black]")
            
            # Context after  
            for line in match.get("context_after", [])[:5]:  # Limit context
                self.console.print(f"[dim]{line}[/dim]")
                
        # Footer with stats
        stats = result.data
        footer_text = f"Found {stats['total_matches']} matches in {stats['total_files_searched']} files"
        if stats.get("truncated"):
            footer_text += " (truncated)"
        footer_text += f" | Search time: {stats['search_time']:.2f}s"
        
        self.console.print(f"\n[dim]{footer_text}[/dim]")
        
    def _display_generic_tool_result(self, tool_name: str, result: ToolResult) -> None:
        """Generic tool result display"""
        
        # Status indicator
        if result.status.value == "success":
            status_style = "green"
            status_icon = "✅"
        elif result.status.value == "error":
            status_style = "red"
            status_icon = "❌"
        else:
            status_style = "yellow"
            status_icon = "⏳"
            
        # Tool header
        header = f"{status_icon} {tool_name.replace('_', ' ').title()}"
        if result.execution_time > 0:
            header += f" [dim]({result.execution_time:.2f}s)[/dim]"
            
        self.console.print(f"[{status_style} bold]{header}[/{status_style} bold]")
        
        # Message
        if result.message:
            self.console.print(f"[{status_style}]{result.message}[/{status_style}]")
        
        # Data (if not too large)
        if result.data and len(str(result.data)) < 2000:
            if isinstance(result.data, (dict, list)):
                # Pretty print JSON
                json_str = json.dumps(result.data, indent=2, default=str)
                syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)
                self.console.print(syntax)
            else:
                self.console.print(str(result.data))
        
        # Error details
        if result.error:
            error_panel = Panel(
                str(result.error),
                title="Error Details",
                border_style="red"
            )
            self.console.print(error_panel)
            
    def format_list_files_safe(self, files_data: Dict[str, Any]) -> Tree:
        """Format file listing without broken line characters - SITUAÇÃO 1 FIX"""
        
        tree = Tree("📁 Files", style="blue bold")
        
        if not files_data or not files_data.get("files"):
            return tree
            
        files = files_data["files"]
        
        # Sort and organize files
        sorted_files = sorted(files, key=lambda x: str(x).lower())
        
        for file_info in sorted_files:
            if isinstance(file_info, dict):
                name = file_info.get("name", file_info.get("path", "unknown"))
                file_type = file_info.get("type", "file")
                size = file_info.get("size")
            else:
                name = str(file_info)
                file_type = "file"
                size = None
                
            # Use simple icons instead of tree characters
            icon = "📁" if file_type == "directory" else "📄"
            
            display_name = f"{icon} {name}"
            if size is not None:
                display_name += f" [dim]({self._format_file_size(size)})[/dim]"
                
            tree.add(display_name)
            
        return tree
        
    def format_search_results_table(self, results: Dict[str, Any]) -> Table:
        """Format search results as a clean table"""
        
        table = Table(title="🔍 Search Results", show_header=True, header_style="blue bold")
        table.add_column("File", style="cyan", no_wrap=True)
        table.add_column("Line", justify="right", style="yellow", width=6)
        table.add_column("Match", style="white")
        
        matches = results.get("matches", [])
        for match in matches[:20]:  # Limit to 20 for display
            file_name = match["file"].split("/")[-1] if "/" in match["file"] else match["file"]
            line_num = str(match["line_number"])
            match_text = match["match_text"][:80] + "..." if len(match["match_text"]) > 80 else match["match_text"]
            
            table.add_row(file_name, line_num, match_text)
            
        return table
        
    def display_plan_progress(self, manifest: Dict[str, Any]) -> None:
        """Display plan execution progress with live updates"""
        
        # Progress bar for overall plan
        total_steps = manifest.get("total_steps", 0)
        completed_steps = len(manifest.get("completed_steps", []))
        
        with Progress() as progress:
            task = progress.add_task("Plan Execution", total=total_steps)
            progress.update(task, completed=completed_steps)
            
        # Current step info
        current_step = manifest.get("current_step", {})
        if current_step:
            step_panel = Panel(
                f"Step {current_step.get('id', 'N/A')}: {current_step.get('description', 'No description')}",
                title="Current Step",
                border_style="yellow"
            )
            self.console.print(step_panel)
            
    def _format_file_size(self, size_bytes: int) -> str:
        """Format file size in human readable format"""
        
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f}{unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f}TB"
        
    def display_tool_status(self, tool_name: str, status: str, message: str = "") -> None:
        """Display tool execution status"""
        
        if status == "running":
            self.console.print(f"⏳ Executing {tool_name}...")
        elif status == "success":
            self.console.print(f"✅ {tool_name} completed successfully")
        elif status == "error":
            self.console.print(f"❌ {tool_name} failed: {message}")
            
        if message and status != "error":
            self.console.print(f"   {message}")
            
    def clear_display(self) -> None:
        """Clear the console display"""
        self.console.clear()
        
    def show_separator(self, title: str = "") -> None:
        """Show a separator line with optional title"""
        if title:
            self.console.rule(f"[blue]{title}[/blue]")
        else:
            self.console.rule()
            
    def show_error(self, error: str, details: Optional[str] = None) -> None:
        """Display error message"""
        error_panel = Panel(
            error + (f"\n\n{details}" if details else ""),
            title="❌ Error",
            border_style="red"
        )
        self.console.print(error_panel)
        
    def show_warning(self, warning: str) -> None:
        """Display warning message"""  
        self.console.print(f"⚠️  [yellow]{warning}[/yellow]")
        
    def show_success(self, message: str) -> None:
        """Display success message"""
        self.console.print(f"✅ [green]{message}[/green]")
        
    def show_info(self, message: str) -> None:
        """Display info message"""
        self.console.print(f"ℹ️  [blue]{message}[/blue]")