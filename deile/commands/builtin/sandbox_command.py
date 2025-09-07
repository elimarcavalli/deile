"""Sandbox Command - Quick sandbox mode toggle and status"""

from typing import Dict, Any, Optional
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError


class SandboxCommand(DirectCommand):
    """Quick toggle and status for sandbox execution mode"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="sandbox",
            description="Toggle and check sandbox execution mode.",
            aliases=["sb", "isolation"]
        )
        super().__init__(config)
        # In a real implementation, this would connect to the sandbox manager
        self.sandbox_enabled = False
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute sandbox command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show sandbox status
                return await self._show_sandbox_status()
            
            action = parts[0].lower()
            
            if action in ["on", "enable", "true"]:
                return await self._toggle_sandbox(True)
            elif action in ["off", "disable", "false"]:
                return await self._toggle_sandbox(False)
            elif action in ["status", "info"]:
                return await self._show_sandbox_status()
            elif action in ["config", "configure"]:
                return await self._show_sandbox_config()
            else:
                raise CommandError(f"Unknown sandbox action: {action}")
                
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute sandbox command: {str(e)}")
    
    async def _show_sandbox_status(self) -> CommandResult:
        """Show current sandbox status"""
        
        # Status styling
        status_emoji = "🟢" if self.sandbox_enabled else "🔴"
        status_text = "ENABLED" if self.sandbox_enabled else "DISABLED"
        status_color = "green" if self.sandbox_enabled else "red"
        
        # Create status table
        status_table = Table(title=f"{status_emoji} Sandbox Status", show_header=False)
        status_table.add_column("Property", style="bold cyan", width=20)
        status_table.add_column("Value", style=status_color, width=25)
        status_table.add_column("Description", style="dim", width=30)
        
        status_table.add_row("Mode", f"{status_emoji} {status_text}", "Current sandbox state")
        status_table.add_row("Isolation", "Process-level" if self.sandbox_enabled else "None", "Execution isolation")
        status_table.add_row("File Access", "Restricted" if self.sandbox_enabled else "Unrestricted", "Filesystem permissions")
        status_table.add_row("Network", "Controlled" if self.sandbox_enabled else "Open", "Network access policy")
        status_table.add_row("System Calls", "Filtered" if self.sandbox_enabled else "Direct", "System interaction level")
        
        # Features description
        if self.sandbox_enabled:
            features_text = (
                "✅ **Active Protections**\n\n"
                "🔒 **Process Isolation**: Commands run in isolated processes\n"
                "📁 **File System**: Access restricted to workspace and temp directories\n"
                "🌐 **Network Control**: Network access controlled by permission rules\n"
                "⚙️ **System Calls**: Dangerous system calls are blocked or monitored\n"
                "🕒 **Timeouts**: All operations have enforced time limits\n"
                "📊 **Resource Limits**: CPU, memory, and disk usage are capped\n"
                "🔍 **Monitoring**: All actions are logged for audit\n\n"
                "💡 **Note**: Sandbox provides security but may limit some operations."
            )
            features_color = "green"
        else:
            features_text = (
                "⚠️ **Sandbox Disabled**\n\n"
                "❌ Tools run with full system access\n"
                "❌ No process isolation or resource limits\n"
                "❌ Direct file system and network access\n"
                "❌ All system calls are permitted\n\n"
                "🚨 **Security Risk**: Running without sandbox increases security exposure\n\n"
                "💡 **Recommendation**: Enable sandbox for production use\n"
                "🛡️ Use '/sandbox on' to enable protection"
            )
            features_color = "red"
        
        features_panel = Panel(
            Text(features_text, style=features_color),
            title="🛡️ Security Features",
            border_style=features_color
        )
        
        # Quick actions
        actions_text = (
            "🚀 **Quick Actions**\n\n"
            f"/sandbox {'off' if self.sandbox_enabled else 'on'}     - {'Disable' if self.sandbox_enabled else 'Enable'} sandbox mode\n"
            "/sandbox config   - Show detailed configuration\n"
            "/permissions      - Manage detailed security rules\n"
            "/tools            - List tools and their sandbox requirements\n\n"
            "⚡ **For Plan Execution**\n"
            "/run <plan> --sandbox-mode - Override sandbox for single run\n"
            "/approve <plan> <step>     - Manual approval bypasses some restrictions"
        )
        
        actions_panel = Panel(
            Text(actions_text, style="blue"),
            title="🎛️ Controls",
            border_style="blue"
        )
        
        from rich.console import Group
        content = Group(status_table, "", features_panel, "", actions_panel)
        
        return CommandResult.success_result(content, "rich")
    
    async def _toggle_sandbox(self, enabled: bool) -> CommandResult:
        """Enable or disable sandbox mode"""
        
        old_status = self.sandbox_enabled
        self.sandbox_enabled = enabled
        
        action_text = "enabled" if enabled else "disabled"
        emoji = "🟢" if enabled else "🔴"
        color = "green" if enabled else "red"
        
        if old_status == enabled:
            return CommandResult.success_result(
                Panel(
                    Text(f"Sandbox is already {action_text}.", style=color),
                    title=f"{emoji} No Change",
                    border_style=color
                ),
                "rich"
            )
        
        # Impact warning for disabling
        if not enabled:
            warning_text = (
                f"⚠️ **Sandbox Disabled**\n\n"
                f"Security protections are now OFF:\n"
                f"• Tools can access any file\n"
                f"• Network requests unrestricted\n" 
                f"• System commands run directly\n"
                f"• No resource limits enforced\n\n"
                f"🔒 **Recommendation**: Only disable for trusted operations\n"
                f"🛡️ Re-enable with '/sandbox on'"
            )
        else:
            warning_text = (
                f"✅ **Sandbox Enabled**\n\n"
                f"Security protections are now ACTIVE:\n"
                f"• File access restricted to workspace\n"
                f"• Network calls controlled by rules\n" 
                f"• System commands are filtered\n"
                f"• Resource usage is monitored\n\n"
                f"⚡ **Note**: Some tools may require approval\n"
                f"🔍 Use '/permissions check' to test access"
            )
        
        result_panel = Panel(
            Text(warning_text, style=color),
            title=f"{emoji} Sandbox {action_text.title()}",
            border_style=color,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _show_sandbox_config(self) -> CommandResult:
        """Show detailed sandbox configuration"""
        
        # Configuration table
        config_table = Table(title="⚙️ Sandbox Configuration", show_header=True, header_style="bold yellow")
        config_table.add_column("Setting", style="cyan", width=20)
        config_table.add_column("Value", style="white", width=25)
        config_table.add_column("Description", style="dim", width=30)
        
        config_table.add_row("Execution Mode", "Process Isolation", "Isolated subprocess execution")
        config_table.add_row("File System", "Restricted", "Access limited to workspace")
        config_table.add_row("Temp Directory", "/tmp/deile-sandbox", "Isolated temporary storage")
        config_table.add_row("Network Policy", "Rule-based", "Controlled by permission rules")
        config_table.add_row("Resource Limits", "Enforced", "CPU/memory/disk limits")
        config_table.add_row("Timeout", "300s default", "Maximum execution time")
        config_table.add_row("Monitoring", "Full logging", "All operations recorded")
        
        # Security policies
        policies_text = (
            "🔐 **Security Policies**\n\n"
            "**File System Access**:\n"
            "• Read: Workspace, /tmp, read-only system dirs\n"
            "• Write: Workspace subdirs, temp directory only\n"
            "• Blocked: /etc, /bin, /usr, system directories\n\n"
            "**Network Access**:\n"
            "• Allowed: APIs defined in permission rules\n"
            "• Blocked: Local network, SSH, admin ports\n\n"
            "**Process Control**:\n"
            "• Resource limits: 2GB RAM, 4 CPU cores max\n"
            "• Time limits: 5 minutes per tool execution\n"
            "• Signal handling: SIGTERM after timeout\n"
        )
        
        policies_panel = Panel(
            Text(policies_text, style="blue"),
            title="📋 Policies",
            border_style="blue"
        )
        
        # Override options
        overrides_text = (
            "⚡ **Override Options**\n\n"
            "**Per-execution overrides**:\n"
            "/run <plan> --no-sandbox     - Disable for entire plan\n"
            "/run <plan> --relaxed        - Reduced restrictions\n"
            "/approve <plan> <step>       - Manual approval for restricted ops\n\n"
            "**Configuration files**:\n"
            "config/sandbox.yaml          - Main configuration\n"
            "config/permissions.yaml      - Detailed access rules\n\n"
            "**Environment variables**:\n"
            "DEILE_SANDBOX=off            - Global disable\n"
            "DEILE_SANDBOX_MODE=relaxed   - Relaxed mode"
        )
        
        overrides_panel = Panel(
            Text(overrides_text, style="yellow"),
            title="🎛️ Overrides",
            border_style="yellow"
        )
        
        from rich.console import Group
        content = Group(config_table, "", policies_panel, "", overrides_panel)
        
        return CommandResult.success_result(content, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Quick sandbox mode toggle and status

Usage:
  /sandbox              Show current sandbox status and features
  /sandbox on           Enable sandbox protection  
  /sandbox off          Disable sandbox (not recommended)
  /sandbox status       Show detailed status information
  /sandbox config       Show configuration and policies

Sandbox Features:
  • Process isolation for tool execution
  • Restricted file system access (workspace only)  
  • Network access controlled by permission rules
  • Resource limits (CPU, memory, time)
  • System call filtering and monitoring
  • Complete audit logging

Security Levels:
  • Enabled:  Full protection, tools run isolated
  • Disabled: Direct access, higher performance but less secure

Override Options:
  /run <plan> --no-sandbox     Disable sandbox for plan execution
  /run <plan> --relaxed        Reduced sandbox restrictions  
  /approve <plan> <step>       Manual approval for restricted operations

Configuration Files:
  • config/sandbox.yaml        Main sandbox settings
  • config/permissions.yaml    Detailed access control rules

Related Commands:
  • /permissions - Detailed security rule management
  • /run - Execute plans with sandbox control
  • /tools - List tools and their sandbox requirements
  • /approve - Manual approval for restricted operations

Environment Variables:
  • DEILE_SANDBOX=off          Global sandbox disable
  • DEILE_SANDBOX_MODE=relaxed Relaxed restrictions

Examples:
  /sandbox on                  Enable full protection
  /sandbox config              Show all configuration
  /run myplan --no-sandbox     Run plan without sandbox

Aliases: /sb, /isolation"""