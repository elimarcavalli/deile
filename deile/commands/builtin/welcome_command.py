"""Welcome Command - Show welcome message and getting started guide"""

from typing import Dict, Any, Optional
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.columns import Columns
from rich.console import Group

from ..base import DirectCommand, CommandResult, CommandContext


class WelcomeCommand(DirectCommand):
    """Show welcome message and getting started guide for new users"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="welcome",
            description="Show welcome message and getting started guide.",
            aliases=["hello", "start", "intro"]
        )
        super().__init__(config)
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute welcome command"""
        
        # Create welcome header
        welcome_text = Text()
        welcome_text.append("🚀 ", style="bright_blue")
        welcome_text.append("Welcome to ", style="white")
        welcome_text.append("D.E.I.L.E. ", style="bold cyan")
        welcome_text.append("v4.0", style="bright_green")
        welcome_text.append("\n\n")
        welcome_text.append("Your AI-powered development assistant with autonomous execution capabilities!", style="dim")
        
        welcome_panel = Panel(
            welcome_text,
            title="[bold bright_blue]🎯 DEILE - Development Environment Intelligence & Learning Engine[/bold bright_blue]",
            border_style="bright_blue",
            padding=(1, 2)
        )
        
        # Quick start guide
        quickstart_table = Table(title="⚡ Quick Start Guide", show_header=True, header_style="bold yellow")
        quickstart_table.add_column("Action", style="cyan", width=25)
        quickstart_table.add_column("Command", style="green", width=20)
        quickstart_table.add_column("Description", style="white", width=30)
        
        quickstart_table.add_row("Get help", "/help", "List all available commands")
        quickstart_table.add_row("System status", "/status", "Check DEILE system status")
        quickstart_table.add_row("Create plan", "/plan create", "Start autonomous workflow")
        quickstart_table.add_row("Execute bash", "/bash <command>", "Run shell commands safely")
        quickstart_table.add_row("Search files", "/find <pattern>", "Search in project files")
        quickstart_table.add_row("View memory", "/memory", "Check memory usage")
        quickstart_table.add_row("Security", "/permissions", "Manage security settings")
        
        # Key features
        features_text = Text()
        features_text.append("🔧 ", style="yellow")
        features_text.append("Autonomous Orchestration", style="bold")
        features_text.append(" - Create and execute multi-step plans\n", style="dim")
        
        features_text.append("🛡️ ", style="red")  
        features_text.append("Security & Permissions", style="bold")
        features_text.append(" - Granular access control and audit logs\n", style="dim")
        
        features_text.append("📊 ", style="blue")
        features_text.append("Rich UI & Monitoring", style="bold")
        features_text.append(" - Beautiful tables, progress bars, and status panels\n", style="dim")
        
        features_text.append("🔍 ", style="green")
        features_text.append("Intelligent File Operations", style="bold")
        features_text.append(" - Smart search, edit, and context awareness\n", style="dim")
        
        features_text.append("💾 ", style="magenta")
        features_text.append("Memory Management", style="bold")
        features_text.append(" - Advanced session state and checkpoint system\n", style="dim")
        
        features_text.append("🚀 ", style="bright_cyan")
        features_text.append("Export & Integration", style="bold")
        features_text.append(" - Export conversations, plans, and artifacts", style="dim")
        
        features_panel = Panel(
            features_text,
            title="✨ Key Features",
            border_style="yellow"
        )
        
        # Common workflows
        workflows_text = Text()
        workflows_text.append("1️⃣ ", style="bright_blue")
        workflows_text.append("Code Analysis & Refactoring\n", style="bold")
        workflows_text.append("   /find 'TODO|FIXME' → /plan create → /run\n\n", style="dim")
        
        workflows_text.append("2️⃣ ", style="bright_green")
        workflows_text.append("Development Workflow\n", style="bold")
        workflows_text.append("   /bash 'git status' → /plan create 'Deploy' → /approve\n\n", style="dim")
        
        workflows_text.append("3️⃣ ", style="bright_red")
        workflows_text.append("Security & Monitoring\n", style="bold")
        workflows_text.append("   /permissions → /logs security → /sandbox on\n\n", style="dim")
        
        workflows_text.append("4️⃣ ", style="bright_yellow")
        workflows_text.append("Session Management\n", style="bold")
        workflows_text.append("   /memory status → /export → /cls reset", style="dim")
        
        workflows_panel = Panel(
            workflows_text,
            title="🔄 Common Workflows",
            border_style="green"
        )
        
        # Pro tips
        tips_text = Text()
        tips_text.append("💡 ", style="yellow")
        tips_text.append("Use '/help <command>' to see detailed help and aliases\n", style="dim")
        
        tips_text.append("🎯 ", style="blue")
        tips_text.append("Type '/' to see available commands (aliases hidden)\n", style="dim")
        
        tips_text.append("📝 ", style="green")
        tips_text.append("Use '@' to autocomplete file paths in commands\n", style="dim")
        
        tips_text.append("🔐 ", style="red")
        tips_text.append("High-risk operations require manual approval (/approve)\n", style="dim")
        
        tips_text.append("💾 ", style="magenta")
        tips_text.append("Save your work with /memory save before major changes\n", style="dim")
        
        tips_text.append("🚨 ", style="bright_red")
        tips_text.append("Use '/cls reset' for a fresh start if things get messy", style="dim")
        
        tips_panel = Panel(
            tips_text,
            title="💡 Pro Tips",
            border_style="magenta"
        )
        
        # Support information  
        support_text = Text()
        support_text.append("📚 ", style="blue")
        support_text.append("Documentation: ", style="bold")
        support_text.append("docs/2.md (Architecture Overview)\n", style="dim")
        
        support_text.append("🔧 ", style="green")
        support_text.append("Debug Mode: ", style="bold")
        support_text.append("/debug on (Enable detailed logging)\n", style="dim")
        
        support_text.append("📊 ", style="yellow")
        support_text.append("System Info: ", style="bold")
        support_text.append("/status (Version, connectivity, tools)\n", style="dim")
        
        support_text.append("💰 ", style="magenta")
        support_text.append("Usage Tracking: ", style="bold")
        support_text.append("/cost (Tokens and estimated costs)", style="dim")
        
        support_panel = Panel(
            support_text,
            title="🆘 Getting Help",
            border_style="cyan"
        )
        
        # Combine all panels
        content = Group(
            welcome_panel,
            "",
            quickstart_table,
            "",
            Columns([features_panel, workflows_panel]),
            "",
            Columns([tips_panel, support_panel])
        )
        
        return CommandResult.success_result(content, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Show welcome message and getting started guide

Usage:
  /welcome              Show complete welcome guide and overview
  /welcome              Same as above (no additional options)

What This Shows:
  • Welcome message and DEILE overview
  • Quick start guide with essential commands
  • Key features and capabilities overview
  • Common workflow examples
  • Pro tips for efficient usage
  • Support and documentation information

Perfect For:
  • First time users getting started
  • Refreshing knowledge of available features
  • Quick reference of common workflows
  • Understanding DEILE's capabilities

The welcome guide provides a comprehensive overview of DEILE's features
including autonomous orchestration, security management, memory control,
and rich UI capabilities.

Related Commands:
  • /help - List all commands
  • /status - System status  
  • /memory - Session management
  • /context - Current context info

Aliases: /hello, /start, /intro"""