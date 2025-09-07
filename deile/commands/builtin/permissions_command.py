"""Permissions Command - Manage security rules and permissions"""

from typing import Dict, Any, Optional, List
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.tree import Tree
from rich.console import Group

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...security.permissions import (
    get_permission_manager, PermissionManager, PermissionRule, 
    PermissionLevel, ResourceType
)


class PermissionsCommand(DirectCommand):
    """Manage security rules and permissions for tools and resources"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="permissions",
            description="Manage security rules and permissions for tools and resources.",
            aliases=["perms", "security"]
        )
        super().__init__(config)
        self.permission_manager = get_permission_manager()
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute permissions command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show permissions overview
                return await self._show_permissions_overview()
            
            action = parts[0].lower()
            
            if action == "list":
                return await self._list_rules(parts[1:])
            elif action == "show":
                if len(parts) < 2:
                    raise CommandError("permissions show requires rule ID: /permissions show <rule_id>")
                return await self._show_rule(parts[1])
            elif action == "check":
                if len(parts) < 4:
                    raise CommandError("permissions check requires: /permissions check <tool> <resource> <action>")
                return await self._check_permission(parts[1], parts[2], parts[3])
            elif action == "add":
                if len(parts) < 6:
                    raise CommandError("permissions add requires: /permissions add <id> <name> <type> <pattern> <level> [tool1,tool2,...]")
                return await self._add_rule(parts[1:])
            elif action == "enable":
                if len(parts) < 2:
                    raise CommandError("permissions enable requires rule ID: /permissions enable <rule_id>")
                return await self._enable_rule(parts[1], True)
            elif action == "disable":
                if len(parts) < 2:
                    raise CommandError("permissions disable requires rule ID: /permissions disable <rule_id>")
                return await self._enable_rule(parts[1], False)
            elif action == "remove":
                if len(parts) < 2:
                    raise CommandError("permissions remove requires rule ID: /permissions remove <rule_id>")
                return await self._remove_rule(parts[1])
            elif action == "audit":
                return await self._show_audit_log(parts[1:])
            elif action == "sandbox":
                if len(parts) < 2:
                    raise CommandError("permissions sandbox requires: /permissions sandbox <on|off|status>")
                return await self._manage_sandbox(parts[1])
            else:
                raise CommandError(f"Unknown permissions action: {action}")
                
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute permissions command: {str(e)}")
    
    async def _show_permissions_overview(self) -> CommandResult:
        """Show overall permissions status and summary"""
        
        # Gather statistics
        total_rules = len(self.permission_manager.rules)
        enabled_rules = len([r for r in self.permission_manager.rules if r.enabled])
        disabled_rules = total_rules - enabled_rules
        
        # Count by resource type
        type_counts = {}
        for rule in self.permission_manager.rules:
            res_type = rule.resource_type.value
            type_counts[res_type] = type_counts.get(res_type, 0) + 1
        
        # Count by permission level
        level_counts = {}
        for rule in self.permission_manager.rules:
            level = rule.permission_level.value
            level_counts[level] = level_counts.get(level, 0) + 1
        
        # Create overview table
        overview_table = Table(title="🛡️ Security & Permissions Overview", show_header=False)
        overview_table.add_column("Metric", style="bold cyan", width=20)
        overview_table.add_column("Value", style="green", width=15)
        overview_table.add_column("Details", style="dim", width=30)
        
        overview_table.add_row("Total Rules", str(total_rules), "Active security rules")
        overview_table.add_row("Enabled", str(enabled_rules), "Currently enforced")
        overview_table.add_row("Disabled", str(disabled_rules), "Temporarily inactive")
        overview_table.add_row("Default Level", self.permission_manager.default_permission.value, "Fallback permission")
        overview_table.add_row("Sandbox", "🟢 Available" if hasattr(self.permission_manager, 'sandbox_enabled') else "🟡 Not configured", "Isolation mode")
        
        # Resource types breakdown
        types_table = Table(title="📁 Protected Resource Types", show_header=True, header_style="bold yellow")
        types_table.add_column("Type", style="cyan")
        types_table.add_column("Rules", style="green", justify="center")
        types_table.add_column("Description", style="dim")
        
        type_descriptions = {
            "file": "Individual files and patterns",
            "directory": "Directory hierarchies", 
            "command": "System commands and tools",
            "network": "Network resources and APIs",
            "system": "System-level operations"
        }
        
        for res_type, count in sorted(type_counts.items()):
            types_table.add_row(
                res_type.title(),
                str(count),
                type_descriptions.get(res_type, "Custom resource type")
            )
        
        # Permission levels breakdown
        levels_table = Table(title="🔐 Permission Levels", show_header=True, header_style="bold red")
        levels_table.add_column("Level", style="red")
        levels_table.add_column("Rules", style="green", justify="center")
        levels_table.add_column("Access Rights", style="dim")
        
        level_descriptions = {
            "none": "No access permitted",
            "read": "Read-only access",
            "write": "Read and write access",
            "execute": "Execute and modify permissions",
            "admin": "Full administrative access"
        }
        
        for level, count in sorted(level_counts.items()):
            levels_table.add_row(
                level.title(),
                str(count),
                level_descriptions.get(level, "Custom access level")
            )
        
        # Usage instructions
        usage_panel = Panel(
            Text(
                "📖 Usage Instructions\n\n"
                "/permissions                    - Show this overview\n"
                "/permissions list [type|level]  - List all rules with optional filter\n"
                "/permissions show <rule_id>     - Show detailed rule information\n"
                "/permissions check <tool> <resource> <action> - Test permission\n"
                "/permissions add <id> <name> <type> <pattern> <level> [tools] - Add rule\n"
                "/permissions enable/disable <rule_id> - Toggle rule status\n"
                "/permissions remove <rule_id>   - Remove rule permanently\n"
                "/permissions audit [limit]      - Show recent permission events\n"
                "/permissions sandbox <on|off>   - Control sandbox mode\n\n"
                "📋 Quick Examples:\n"
                "/permissions check bash_execute /etc/passwd read\n"
                "/permissions list file\n"
                "/permissions sandbox on",
                style="dim"
            ),
            title="Commands Reference",
            border_style="blue"
        )
        
        # Combine all content
        content = Group(overview_table, "", types_table, "", levels_table, "", usage_panel)
        
        return CommandResult.success_result(content, "rich")
    
    async def _list_rules(self, filters: List[str]) -> CommandResult:
        """List permission rules with optional filtering"""
        
        rules = self.permission_manager.rules
        filter_type = filters[0] if filters else None
        
        # Apply filters
        if filter_type:
            if filter_type in [rt.value for rt in ResourceType]:
                rules = [r for r in rules if r.resource_type.value == filter_type]
            elif filter_type in [pl.value for pl in PermissionLevel]:
                rules = [r for r in rules if r.permission_level.value == filter_type]
            else:
                # Filter by rule name or ID
                rules = [r for r in rules if filter_type.lower() in r.id.lower() or filter_type.lower() in r.name.lower()]
        
        if not rules:
            return CommandResult.success_result(
                Panel(
                    Text(f"No permission rules found" + (f" matching filter: {filter_type}" if filter_type else ""), 
                         style="yellow"),
                    title="🔍 No Results",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create rules table
        table = Table(
            title=f"🛡️ Permission Rules" + (f" (filtered: {filter_type})" if filter_type else f" ({len(rules)} total)"),
            show_header=True, 
            header_style="bold cyan"
        )
        table.add_column("ID", style="cyan", width=15)
        table.add_column("Name", style="white", width=20)
        table.add_column("Type", style="yellow", width=8)
        table.add_column("Level", style="red", width=8)
        table.add_column("Tools", style="green", width=15)
        table.add_column("Status", style="blue", width=8)
        table.add_column("Priority", style="magenta", width=8)
        
        for rule in sorted(rules, key=lambda r: r.priority):
            # Status emoji
            status_emoji = "✅" if rule.enabled else "❌"
            status_text = f"{status_emoji} {'On' if rule.enabled else 'Off'}"
            
            # Tools list (truncate if too long)
            tools_text = ", ".join(rule.tool_names[:2])
            if len(rule.tool_names) > 2:
                tools_text += f" +{len(rule.tool_names) - 2}"
            if "*" in rule.tool_names:
                tools_text = "* (all)"
            
            # Truncate long names
            name = rule.name[:18] + "..." if len(rule.name) > 18 else rule.name
            rule_id = rule.id[:13] + "..." if len(rule.id) > 13 else rule.id
            
            table.add_row(
                rule_id,
                name,
                rule.resource_type.value.title(),
                rule.permission_level.value.title(),
                tools_text,
                status_text,
                str(rule.priority)
            )
        
        return CommandResult.success_result(table, "rich")
    
    async def _show_rule(self, rule_id: str) -> CommandResult:
        """Show detailed information about a specific rule"""
        
        rule = self.permission_manager.get_rule(rule_id)
        if not rule:
            raise CommandError(f"Rule '{rule_id}' not found")
        
        # Rule details table
        details_table = Table(title=f"🛡️ Rule Details: {rule.name}", show_header=False)
        details_table.add_column("Property", style="bold cyan", width=18)
        details_table.add_column("Value", style="white", width=40)
        details_table.add_column("Description", style="dim", width=25)
        
        details_table.add_row("ID", rule.id, "Unique identifier")
        details_table.add_row("Name", rule.name, "Human-readable name")
        details_table.add_row("Description", rule.description, "Rule purpose")
        details_table.add_row("Resource Type", rule.resource_type.value, "What this rule protects")
        details_table.add_row("Pattern", rule.resource_pattern, "Matching regex pattern")
        details_table.add_row("Permission Level", rule.permission_level.value, "Access level granted")
        details_table.add_row("Priority", str(rule.priority), "Rule precedence (lower = higher)")
        details_table.add_row("Status", "✅ Enabled" if rule.enabled else "❌ Disabled", "Current state")
        details_table.add_row("Tools", ", ".join(rule.tool_names) if "*" not in rule.tool_names else "* (all tools)", "Applicable tools")
        
        # Conditions (if any)
        if rule.conditions:
            conditions_text = "\n".join([f"{k}: {v}" for k, v in rule.conditions.items()])
            details_table.add_row("Conditions", conditions_text, "Additional constraints")
        
        # Test examples
        examples_panel = Panel(
            Text(
                f"🧪 Test Examples:\n\n"
                f"/permissions check write_file '/path/file.txt' write\n"
                f"/permissions check bash_execute '/bin/ls' execute\n"
                f"/permissions check {rule.tool_names[0] if rule.tool_names and rule.tool_names[0] != '*' else 'any_tool'} <resource> <action>\n\n"
                f"Pattern will match resources like:\n"
                f"• Regex: {rule.resource_pattern}\n"
                f"• Example matches: depends on pattern complexity",
                style="dim"
            ),
            title="Testing This Rule",
            border_style="green"
        )
        
        content = Group(details_table, "", examples_panel)
        
        return CommandResult.success_result(content, "rich")
    
    async def _check_permission(self, tool: str, resource: str, action: str) -> CommandResult:
        """Check if a specific permission is allowed"""
        
        # Perform the permission check
        allowed = self.permission_manager.check_permission(tool, resource, action)
        
        # Find which rule was applied
        applicable_rules = [
            rule for rule in sorted(self.permission_manager.rules, key=lambda r: r.priority)
            if rule.enabled and rule.applies_to_tool(tool) and rule.matches_resource(resource)
        ]
        
        applied_rule = applicable_rules[0] if applicable_rules else None
        
        # Result styling
        result_emoji = "✅" if allowed else "❌"
        result_color = "green" if allowed else "red"
        result_text = "ALLOWED" if allowed else "DENIED"
        
        # Create result table
        result_table = Table(title=f"{result_emoji} Permission Check Result", show_header=False)
        result_table.add_column("Property", style="bold cyan", width=15)
        result_table.add_column("Value", style=result_color, width=30)
        result_table.add_column("Details", style="dim", width=25)
        
        result_table.add_row("Tool", tool, "Requesting tool")
        result_table.add_row("Resource", resource, "Target resource")
        result_table.add_row("Action", action, "Requested action")
        result_table.add_row("Result", f"{result_emoji} {result_text}", "Final decision")
        
        if applied_rule:
            result_table.add_row("Applied Rule", applied_rule.id, "Rule that made decision")
            result_table.add_row("Rule Priority", str(applied_rule.priority), "Rule precedence")
            result_table.add_row("Permission Level", applied_rule.permission_level.value, "Granted access level")
        else:
            result_table.add_row("Applied Rule", "Default", "No specific rule matched")
            result_table.add_row("Default Level", self.permission_manager.default_permission.value, "Fallback permission")
        
        # Explanation panel
        if allowed:
            explanation = f"✅ **Access Granted**\n\nThe tool '{tool}' is permitted to perform '{action}' on resource '{resource}'."
            if applied_rule:
                explanation += f"\n\n🛡️ **Applied Rule**: {applied_rule.name}\n📝 **Description**: {applied_rule.description}"
                explanation += f"\n🔢 **Priority**: {applied_rule.priority} (rules with lower numbers take precedence)"
            else:
                explanation += f"\n\n🔄 **Default Permission**: {self.permission_manager.default_permission.value}\n📝 No specific rules matched this request."
        else:
            explanation = f"❌ **Access Denied**\n\nThe tool '{tool}' is NOT permitted to perform '{action}' on resource '{resource}'."
            if applied_rule:
                explanation += f"\n\n🚫 **Blocking Rule**: {applied_rule.name}\n📝 **Description**: {applied_rule.description}"
                explanation += f"\n🔢 **Priority**: {applied_rule.priority}\n💡 **Tip**: You can modify the rule with '/permissions show {applied_rule.id}'"
            else:
                explanation += f"\n\n🔄 **Default Permission**: {self.permission_manager.default_permission.value}\n📝 Default settings deny this action."
        
        explanation_panel = Panel(
            Text(explanation, style=result_color),
            title="📋 Explanation",
            border_style=result_color
        )
        
        content = Group(result_table, "", explanation_panel)
        
        return CommandResult.success_result(content, "rich")
    
    async def _add_rule(self, args: List[str]) -> CommandResult:
        """Add a new permission rule"""
        # This is a simplified version - in production you'd want more validation
        raise CommandError("Rule creation not implemented in this demo. Use configuration files to add rules.")
    
    async def _enable_rule(self, rule_id: str, enabled: bool) -> CommandResult:
        """Enable or disable a rule"""
        
        rule = self.permission_manager.get_rule(rule_id)
        if not rule:
            raise CommandError(f"Rule '{rule_id}' not found")
        
        old_status = rule.enabled
        rule.enabled = enabled
        
        action_text = "enabled" if enabled else "disabled"
        emoji = "✅" if enabled else "❌"
        color = "green" if enabled else "red"
        
        if old_status == enabled:
            return CommandResult.success_result(
                Panel(
                    Text(f"Rule '{rule_id}' is already {action_text}.", style=color),
                    title=f"{emoji} No Change",
                    border_style=color
                ),
                "rich"
            )
        
        # Success message
        content_lines = [
            f"{emoji} **Rule {action_text.title()} Successfully**",
            "",
            f"**Rule ID**: {rule_id}",
            f"**Rule Name**: {rule.name}",
            f"**Description**: {rule.description}",
            f"**Resource Type**: {rule.resource_type.value}",
            f"**Permission Level**: {rule.permission_level.value}",
            f"**Priority**: {rule.priority}",
            "",
            f"**Status**: {action_text.title()}",
            f"**Effect**: This rule is now {'active' if enabled else 'inactive'} and {'will' if enabled else 'will not'} be enforced."
        ]
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style=color),
            title=f"⚡ Rule {action_text.title()}",
            border_style=color,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _remove_rule(self, rule_id: str) -> CommandResult:
        """Remove a rule"""
        # This is a simplified version - in production you'd want confirmation
        raise CommandError("Rule removal not implemented in this demo. Use configuration files to remove rules.")
    
    async def _show_audit_log(self, args: List[str]) -> CommandResult:
        """Show permission audit log"""
        # This would show recent permission checks and their results
        return CommandResult.success_result(
            Panel(
                Text("Audit logging not yet implemented. Will show recent permission checks, denials, and security events.", 
                     style="yellow"),
                title="🔍 Audit Log",
                border_style="yellow"
            ),
            "rich"
        )
    
    async def _manage_sandbox(self, mode: str) -> CommandResult:
        """Manage sandbox mode"""
        
        if mode.lower() == "status":
            # Show current sandbox status
            content = Panel(
                Text("Sandbox enforcement will be integrated with tool execution system.\n\n"
                     "Features:\n"
                     "• Isolated execution environments\n"
                     "• Resource access controls\n" 
                     "• Network restrictions\n"
                     "• Temporary file systems\n\n"
                     "Status: Available for integration", 
                     style="blue"),
                title="🏗️ Sandbox Status",
                border_style="blue"
            )
            return CommandResult.success_result(content, "rich")
        elif mode.lower() in ["on", "enable"]:
            return CommandResult.success_result(
                Panel(
                    Text("Sandbox mode configuration will be integrated with execution system.", 
                         style="green"),
                    title="✅ Sandbox Configuration",
                    border_style="green"
                ),
                "rich"
            )
        elif mode.lower() in ["off", "disable"]:
            return CommandResult.success_result(
                Panel(
                    Text("Sandbox mode would be disabled (not recommended for production).", 
                         style="yellow"),
                    title="⚠️ Sandbox Disabled",
                    border_style="yellow"
                ),
                "rich"
            )
        else:
            raise CommandError(f"Invalid sandbox mode: {mode}. Use 'on', 'off', or 'status'.")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Manage security rules and permissions for tools and resources

Usage:
  /permissions                           Show permissions overview and status
  /permissions list [filter]             List all rules (filter by type/level/name)
  /permissions show <rule_id>            Show detailed rule information
  /permissions check <tool> <resource> <action>  Test permission check
  /permissions enable <rule_id>          Enable a rule
  /permissions disable <rule_id>         Disable a rule
  /permissions audit [limit]             Show recent permission events
  /permissions sandbox <on|off|status>   Manage sandbox mode

Filters for 'list':
  file, directory, command, network, system    - Filter by resource type
  none, read, write, execute, admin            - Filter by permission level
  any_text                                     - Filter by rule name/ID

Permission Check Examples:
  /permissions check bash_execute "/bin/ls" execute
  /permissions check write_file "config.yaml" write
  /permissions check read_file "/etc/passwd" read

Resource Types:
  • file      - Individual files and file patterns
  • directory - Directory trees and paths
  • command   - System commands and executables
  • network   - Network endpoints and APIs
  • system    - System-level resources

Permission Levels:
  • none      - No access allowed
  • read      - Read-only access
  • write     - Read and write access  
  • execute   - Execute and administrative access
  • admin     - Full administrative access

Security Features:
  • Rule-based access control with priority system
  • Pattern matching for flexible resource definitions
  • Tool-specific permission enforcement
  • Sandbox isolation support
  • Audit logging of permission events
  • Default permission fallback

Related Commands:
  • /sandbox - Quick sandbox toggle
  • /tools - List available tools and their requirements
  • /run - Execute plans with permission enforcement
  • /approve - Manual approval for high-risk operations

Aliases: /perms, /security"""