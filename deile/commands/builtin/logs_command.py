"""Logs Command - View security audit logs and system events"""

from typing import Dict, Any, Optional, List
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.tree import Tree
from rich.console import Group
from datetime import datetime, timedelta

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...security.audit_logger import (
    get_audit_logger, AuditEventType, SeverityLevel
)


class LogsCommand(DirectCommand):
    """View security audit logs and system events"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="logs",
            description="View security audit logs and system events.",
            aliases=["log", "audit"]
        )
        super().__init__(config)
        self.audit_logger = get_audit_logger()
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute logs command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show recent logs overview
                return await self._show_logs_overview()
            
            action = parts[0].lower()
            
            if action == "recent":
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
                return await self._show_recent_logs(limit)
            elif action == "security":
                return await self._show_security_logs(parts[1:])
            elif action == "permissions":
                return await self._show_permission_logs(parts[1:])
            elif action == "secrets":
                return await self._show_secret_logs(parts[1:])
            elif action == "tools":
                return await self._show_tool_logs(parts[1:])
            elif action == "plans":
                return await self._show_plan_logs(parts[1:])
            elif action == "errors":
                return await self._show_error_logs(parts[1:])
            elif action == "summary":
                return await self._show_summary()
            elif action == "export":
                if len(parts) < 2:
                    raise CommandError("logs export requires filename: /logs export <filename> [format]")
                format_type = parts[2] if len(parts) > 2 else "json"
                return await self._export_logs(parts[1], format_type)
            elif action == "clear":
                return await self._clear_logs()
            else:
                raise CommandError(f"Unknown logs action: {action}")
                
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute logs command: {str(e)}")
    
    async def _show_logs_overview(self) -> CommandResult:
        """Show logs overview and recent activity"""
        
        summary = self.audit_logger.get_security_summary()
        recent_events = self.audit_logger.get_recent_events(20)
        
        # Overview stats
        overview_table = Table(title="📊 Audit Logs Overview", show_header=False)
        overview_table.add_column("Metric", style="bold cyan", width=20)
        overview_table.add_column("Value", style="green", width=15)
        overview_table.add_column("Details", style="dim", width=30)
        
        overview_table.add_row("Total Events", str(summary["total_events"]), "In current session")
        overview_table.add_row("Session ID", summary["session_id"], "Current session identifier")
        overview_table.add_row("Permission Denials", str(summary["permission_denials"]), "Access denied events")
        overview_table.add_row("Secret Detections", str(summary["secret_detections"]), "Sensitive data found")
        overview_table.add_row("Critical Events", str(summary["recent_critical_events"]), "Errors and warnings")
        overview_table.add_row("Log File", summary["log_file"], "Persistent storage location")
        
        # Recent activity (last 10 events)
        if recent_events:
            activity_table = Table(title="⚡ Recent Activity (Last 10 events)", show_header=True, header_style="bold yellow")
            activity_table.add_column("Time", style="cyan", width=8)
            activity_table.add_column("Type", style="yellow", width=12)
            activity_table.add_column("Actor", style="green", width=12)
            activity_table.add_column("Action", style="white", width=10)
            activity_table.add_column("Resource", style="blue", width=20)
            activity_table.add_column("Result", style="red", width=8)
            
            for event in recent_events[:10]:
                # Format time (relative)
                time_diff = datetime.now() - event.timestamp
                if time_diff.total_seconds() < 60:
                    time_str = f"{int(time_diff.total_seconds())}s"
                elif time_diff.total_seconds() < 3600:
                    time_str = f"{int(time_diff.total_seconds() / 60)}m"
                else:
                    time_str = event.timestamp.strftime("%H:%M")
                
                # Truncate long strings
                actor = event.actor[:10] + "..." if len(event.actor) > 10 else event.actor
                resource = event.resource[:18] + "..." if len(event.resource) > 18 else event.resource
                
                # Color code result
                result_color = "green" if event.result in ["success", "allowed", "completed"] else "red" if event.result in ["denied", "failed", "error"] else "yellow"
                
                activity_table.add_row(
                    time_str,
                    event.event_type.value.replace("_", " ").title()[:12],
                    actor,
                    event.action.title(),
                    resource,
                    f"[{result_color}]{event.result}[/{result_color}]"
                )
        else:
            activity_table = Panel(
                Text("No recent activity to display.", style="dim"),
                title="⚡ Recent Activity",
                border_style="dim"
            )
        
        # Event type breakdown
        type_counts = summary.get("event_types", {})
        if type_counts:
            types_table = Table(title="📋 Event Types", show_header=True, header_style="bold blue")
            types_table.add_column("Event Type", style="blue")
            types_table.add_column("Count", style="green", justify="center")
            types_table.add_column("Description", style="dim")
            
            type_descriptions = {
                "permission_check": "Access control validation",
                "permission_denied": "Access denied events",
                "secret_detected": "Sensitive data found",
                "secret_redacted": "Data sanitized",
                "tool_execution": "Tool run events",
                "plan_execution": "Plan workflow events",
                "approval_required": "Manual approval needed",
                "sandbox_violation": "Security policy violation"
            }
            
            for event_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
                description = type_descriptions.get(event_type, "Custom event type")
                types_table.add_row(
                    event_type.replace("_", " ").title(),
                    str(count),
                    description
                )
        else:
            types_table = Panel(
                Text("No events recorded yet.", style="dim"),
                title="📋 Event Types", 
                border_style="dim"
            )
        
        # Quick commands
        commands_panel = Panel(
            Text(
                "🚀 **Quick Commands**\n\n"
                "/logs recent [N]        - Show N most recent events\n"
                "/logs security          - Security-related events only\n"
                "/logs permissions       - Permission checks and denials\n"
                "/logs secrets           - Secret detection events\n"
                "/logs tools             - Tool execution logs\n"
                "/logs plans             - Plan execution logs\n"
                "/logs errors            - Errors and warnings only\n"
                "/logs summary           - Detailed statistics\n"
                "/logs export <file>     - Export logs to file\n\n"
                "📊 **Filters Available**\n"
                "Type: permission, secret, tool, plan, approval, sandbox\n"
                "Severity: debug, info, warning, error, critical\n"
                "Actor: tool_name, user, system",
                style="dim"
            ),
            title="📖 Usage Guide",
            border_style="blue"
        )
        
        # Combine all content
        content = Group(overview_table, "", activity_table, "", types_table, "", commands_panel)
        
        return CommandResult.success_result(content, "rich")
    
    async def _show_recent_logs(self, limit: int) -> CommandResult:
        """Show recent log entries"""
        
        events = self.audit_logger.get_recent_events(limit)
        
        if not events:
            return CommandResult.success_result(
                Panel(
                    Text("No log events found.", style="yellow"),
                    title="📄 Recent Logs",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create detailed log table
        log_table = Table(
            title=f"📄 Recent Logs ({len(events)} events)",
            show_header=True,
            header_style="bold cyan"
        )
        log_table.add_column("Timestamp", style="dim", width=19)
        log_table.add_column("Severity", style="red", width=8)
        log_table.add_column("Type", style="yellow", width=15)
        log_table.add_column("Actor", style="green", width=12)
        log_table.add_column("Action", style="white", width=10)
        log_table.add_column("Resource", style="blue", width=25)
        log_table.add_column("Result", style="magenta", width=10)
        
        for event in events:
            # Severity styling
            severity_emoji = {
                SeverityLevel.DEBUG: "🔍",
                SeverityLevel.INFO: "ℹ️",
                SeverityLevel.WARNING: "⚠️",
                SeverityLevel.ERROR: "❌",
                SeverityLevel.CRITICAL: "🚨"
            }.get(event.severity, "📝")
            
            # Truncate long values
            actor = event.actor[:10] + "..." if len(event.actor) > 10 else event.actor
            resource = event.resource[:23] + "..." if len(event.resource) > 23 else event.resource
            event_type = event.event_type.value.replace("_", " ")[:13]
            
            log_table.add_row(
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                f"{severity_emoji} {event.severity.value[:4].upper()}",
                event_type,
                actor,
                event.action,
                resource,
                event.result
            )
        
        return CommandResult.success_result(log_table, "rich")
    
    async def _show_security_logs(self, filters: List[str]) -> CommandResult:
        """Show security-related logs"""
        
        security_events = [
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SECRET_DETECTED,
            AuditEventType.SECRET_REDACTED,
            AuditEventType.SANDBOX_VIOLATION,
            AuditEventType.SUSPICIOUS_ACTIVITY
        ]
        
        all_events = []
        for event_type in security_events:
            events = self.audit_logger.get_recent_events(event_type=event_type)
            all_events.extend(events)
        
        # Sort by timestamp, most recent first
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        
        if not all_events:
            return CommandResult.success_result(
                Panel(
                    Text("No security events found. ✅ System appears secure.", style="green"),
                    title="🛡️ Security Logs",
                    border_style="green"
                ),
                "rich"
            )
        
        # Security events table
        security_table = Table(
            title=f"🛡️ Security Events ({len(all_events)} total)",
            show_header=True,
            header_style="bold red"
        )
        security_table.add_column("Time", style="cyan", width=8)
        security_table.add_column("Event", style="red", width=15)
        security_table.add_column("Severity", style="yellow", width=8)
        security_table.add_column("Details", style="white", width=40)
        security_table.add_column("Action Taken", style="green", width=15)
        
        for event in all_events[:50]:  # Limit to 50 most recent
            # Format time
            time_diff = datetime.now() - event.timestamp
            if time_diff.total_seconds() < 3600:
                time_str = f"{int(time_diff.total_seconds() / 60)}m ago"
            else:
                time_str = event.timestamp.strftime("%H:%M")
            
            # Event details
            if event.event_type == AuditEventType.PERMISSION_DENIED:
                details = f"{event.actor} → {event.resource} ({event.action})"
                action = "Access blocked"
            elif event.event_type in [AuditEventType.SECRET_DETECTED, AuditEventType.SECRET_REDACTED]:
                secret_type = event.details.get("secret_type", "unknown")
                line = event.details.get("line_number", "?")
                details = f"{secret_type} in {event.resource}:{line}"
                action = "Redacted" if event.event_type == AuditEventType.SECRET_REDACTED else "Detected"
            else:
                details = f"{event.resource} by {event.actor}"
                action = event.result.title()
            
            security_table.add_row(
                time_str,
                event.event_type.value.replace("_", " ").title(),
                f"{event.severity.value.upper()}",
                details[:38] + "..." if len(details) > 38 else details,
                action
            )
        
        return CommandResult.success_result(security_table, "rich")
    
    async def _show_permission_logs(self, filters: List[str]) -> CommandResult:
        """Show permission-related logs"""
        
        permission_events = self.audit_logger.get_recent_events(event_type=AuditEventType.PERMISSION_CHECK)
        denied_events = self.audit_logger.get_recent_events(event_type=AuditEventType.PERMISSION_DENIED)
        
        all_events = permission_events + denied_events
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        
        if not all_events:
            return CommandResult.success_result(
                Panel(
                    Text("No permission events found.", style="blue"),
                    title="🔐 Permission Logs",
                    border_style="blue"
                ),
                "rich"
            )
        
        # Stats summary
        total_checks = len(permission_events)
        total_denials = len(denied_events)
        denial_rate = (total_denials / (total_checks + total_denials) * 100) if (total_checks + total_denials) > 0 else 0
        
        stats_text = f"**Permission Statistics**\n\nTotal Checks: {total_checks}\nDenied: {total_denials}\nDenial Rate: {denial_rate:.1f}%"
        
        stats_panel = Panel(
            Text(stats_text, style="cyan"),
            title="📊 Stats",
            border_style="cyan"
        )
        
        # Permission events table  
        perm_table = Table(
            title=f"🔐 Permission Events (Last 30)",
            show_header=True,
            header_style="bold blue"
        )
        perm_table.add_column("Time", style="dim", width=8)
        perm_table.add_column("Tool", style="green", width=15)
        perm_table.add_column("Resource", style="blue", width=25)
        perm_table.add_column("Action", style="white", width=10)
        perm_table.add_column("Result", style="red", width=10)
        perm_table.add_column("Rule", style="yellow", width=15)
        
        for event in all_events[:30]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            
            # Color code result
            if event.result == "allowed":
                result_display = "[green]✅ ALLOW[/green]"
            else:
                result_display = "[red]❌ DENY[/red]"
            
            rule_id = event.details.get("rule_id", "default")[:13]
            
            perm_table.add_row(
                time_str,
                event.actor[:13],
                event.resource[:23] + "..." if len(event.resource) > 23 else event.resource,
                event.action,
                result_display,
                rule_id
            )
        
        content = Group(stats_panel, "", perm_table)
        
        return CommandResult.success_result(content, "rich")
    
    async def _show_secret_logs(self, filters: List[str]) -> CommandResult:
        """Show secret detection logs"""
        
        secret_events = []
        for event_type in [AuditEventType.SECRET_DETECTED, AuditEventType.SECRET_REDACTED]:
            events = self.audit_logger.get_recent_events(event_type=event_type)
            secret_events.extend(events)
        
        if not secret_events:
            return CommandResult.success_result(
                Panel(
                    Text("No secret detection events found. ✅ No sensitive data detected.", style="green"),
                    title="🔐 Secret Detection Logs",
                    border_style="green"
                ),
                "rich"
            )
        
        # Secrets table
        secrets_table = Table(
            title=f"🔐 Secret Detection Events ({len(secret_events)} total)",
            show_header=True,
            header_style="bold red"
        )
        secrets_table.add_column("Time", style="dim", width=19)
        secrets_table.add_column("File", style="blue", width=25)
        secrets_table.add_column("Secret Type", style="red", width=15)
        secrets_table.add_column("Line", style="yellow", width=6, justify="center")
        secrets_table.add_column("Confidence", style="green", width=10, justify="center")
        secrets_table.add_column("Action", style="magenta", width=10)
        
        for event in sorted(secret_events, key=lambda e: e.timestamp, reverse=True):
            file_path = event.resource.split("/")[-1] if "/" in event.resource else event.resource
            secret_type = event.details.get("secret_type", "unknown")
            line_number = event.details.get("line_number", "?")
            confidence = event.details.get("confidence", 0.0)
            
            action = "🔒 Redacted" if event.event_type == AuditEventType.SECRET_REDACTED else "⚠️ Detected"
            
            secrets_table.add_row(
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                file_path[:23] + "..." if len(file_path) > 23 else file_path,
                secret_type.title(),
                str(line_number),
                f"{confidence:.2f}",
                action
            )
        
        return CommandResult.success_result(secrets_table, "rich")
    
    async def _show_tool_logs(self, filters: List[str]) -> CommandResult:
        """Show tool execution logs"""
        
        tool_events = self.audit_logger.get_recent_events(event_type=AuditEventType.TOOL_EXECUTION)
        
        if not tool_events:
            return CommandResult.success_result(
                Panel(
                    Text("No tool execution events found.", style="blue"),
                    title="🔧 Tool Execution Logs",
                    border_style="blue"
                ),
                "rich"
            )
        
        # Tool execution stats
        successful_runs = len([e for e in tool_events if e.result == "success"])
        failed_runs = len([e for e in tool_events if e.result == "failure"])
        success_rate = (successful_runs / len(tool_events) * 100) if tool_events else 0
        
        # Tools table
        tools_table = Table(
            title=f"🔧 Tool Execution Logs ({len(tool_events)} runs, {success_rate:.1f}% success)",
            show_header=True,
            header_style="bold green"
        )
        tools_table.add_column("Time", style="dim", width=8)
        tools_table.add_column("Tool", style="green", width=15)
        tools_table.add_column("Resource", style="blue", width=25)
        tools_table.add_column("Duration", style="yellow", width=10)
        tools_table.add_column("Exit Code", style="cyan", width=10)
        tools_table.add_column("Result", style="red", width=10)
        
        for event in sorted(tool_events, key=lambda e: e.timestamp, reverse=True)[:30]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            
            duration_ms = event.details.get("duration_ms")
            duration_str = f"{duration_ms}ms" if duration_ms else "N/A"
            
            exit_code = event.details.get("exit_code", "N/A")
            
            result_color = "green" if event.result == "success" else "red"
            result_display = f"[{result_color}]{event.result.upper()}[/{result_color}]"
            
            tools_table.add_row(
                time_str,
                event.tool_name or event.actor,
                event.resource[:23] + "..." if len(event.resource) > 23 else event.resource,
                duration_str,
                str(exit_code),
                result_display
            )
        
        return CommandResult.success_result(tools_table, "rich")
    
    async def _show_plan_logs(self, filters: List[str]) -> CommandResult:
        """Show plan execution logs"""
        
        plan_events = self.audit_logger.get_recent_events(event_type=AuditEventType.PLAN_EXECUTION)
        approval_events = []
        for event_type in [AuditEventType.APPROVAL_REQUIRED, AuditEventType.APPROVAL_GRANTED, AuditEventType.APPROVAL_DENIED]:
            events = self.audit_logger.get_recent_events(event_type=event_type)
            approval_events.extend(events)
        
        all_events = plan_events + approval_events
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        
        if not all_events:
            return CommandResult.success_result(
                Panel(
                    Text("No plan execution events found.", style="blue"),
                    title="📋 Plan Execution Logs",
                    border_style="blue"
                ),
                "rich"
            )
        
        # Plans table
        plans_table = Table(
            title=f"📋 Plan Execution Logs ({len(all_events)} events)",
            show_header=True,
            header_style="bold purple"
        )
        plans_table.add_column("Time", style="dim", width=8)
        plans_table.add_column("Plan ID", style="purple", width=12)
        plans_table.add_column("Event", style="yellow", width=15)
        plans_table.add_column("Action", style="white", width=12)
        plans_table.add_column("Result", style="green", width=12)
        plans_table.add_column("Details", style="blue", width=25)
        
        for event in all_events[:30]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            
            plan_id = event.plan_id or "N/A"
            if len(plan_id) > 12:
                plan_id = plan_id[:9] + "..."
            
            event_type = event.event_type.value.replace("_", " ").title()
            
            # Extract details based on event type
            if event.event_type == AuditEventType.PLAN_EXECUTION:
                details = f"Steps: {event.details.get('step_count', 'N/A')}"
            elif "approval" in event.event_type.value:
                step_id = event.details.get('step_id', 'N/A')
                risk = event.details.get('risk_level', 'N/A')
                details = f"Step: {step_id}, Risk: {risk}"
            else:
                details = "N/A"
            
            plans_table.add_row(
                time_str,
                plan_id,
                event_type[:15],
                event.action.title(),
                event.result.title(),
                details[:23] + "..." if len(details) > 23 else details
            )
        
        return CommandResult.success_result(plans_table, "rich")
    
    async def _show_error_logs(self, filters: List[str]) -> CommandResult:
        """Show error and warning logs"""
        
        error_events = []
        for severity in [SeverityLevel.WARNING, SeverityLevel.ERROR, SeverityLevel.CRITICAL]:
            events = self.audit_logger.get_recent_events(severity=severity)
            error_events.extend(events)
        
        error_events.sort(key=lambda e: e.timestamp, reverse=True)
        
        if not error_events:
            return CommandResult.success_result(
                Panel(
                    Text("No errors or warnings found. ✅ System running smoothly.", style="green"),
                    title="❌ Error Logs",
                    border_style="green"
                ),
                "rich"
            )
        
        # Errors table
        errors_table = Table(
            title=f"❌ Errors & Warnings ({len(error_events)} events)",
            show_header=True,
            header_style="bold red"
        )
        errors_table.add_column("Time", style="dim", width=19)
        errors_table.add_column("Severity", style="red", width=8)
        errors_table.add_column("Type", style="yellow", width=15)
        errors_table.add_column("Actor", style="green", width=12)
        errors_table.add_column("Error Details", style="white", width=30)
        
        for event in error_events[:30]:
            severity_emoji = {
                SeverityLevel.WARNING: "⚠️",
                SeverityLevel.ERROR: "❌",
                SeverityLevel.CRITICAL: "🚨"
            }.get(event.severity, "❓")
            
            # Construct error details
            details = f"{event.resource} - {event.action} {event.result}"
            
            errors_table.add_row(
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                f"{severity_emoji} {event.severity.value.upper()}",
                event.event_type.value.replace("_", " ").title()[:15],
                event.actor[:12],
                details[:28] + "..." if len(details) > 28 else details
            )
        
        return CommandResult.success_result(errors_table, "rich")
    
    async def _show_summary(self) -> CommandResult:
        """Show detailed audit log summary"""
        
        summary = self.audit_logger.get_security_summary()
        recent_events = self.audit_logger.get_recent_events(1000)  # Larger sample
        
        # Detailed statistics
        stats_table = Table(title="📊 Detailed Audit Statistics", show_header=False)
        stats_table.add_column("Metric", style="bold cyan", width=25)
        stats_table.add_column("Value", style="green", width=15)
        stats_table.add_column("Percentage", style="yellow", width=15)
        
        total_events = len(recent_events)
        
        # Event type breakdown
        type_counts = {}
        for event in recent_events:
            event_type = event.event_type.value
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
        
        for event_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / total_events * 100) if total_events > 0 else 0
            stats_table.add_row(
                event_type.replace("_", " ").title(),
                str(count),
                f"{percentage:.1f}%"
            )
        
        return CommandResult.success_result(stats_table, "rich")
    
    async def _export_logs(self, filename: str, format_type: str) -> CommandResult:
        """Export logs to file"""
        
        try:
            exported_file = self.audit_logger.export_audit_log(filename, format_type)
            
            return CommandResult.success_result(
                Panel(
                    Text(
                        f"✅ **Logs Exported Successfully**\n\n"
                        f"**File**: {exported_file}\n"
                        f"**Format**: {format_type.upper()}\n"
                        f"**Events**: {len(self.audit_logger.recent_events)}\n\n"
                        f"The exported file contains all audit events from the current session.",
                        style="green"
                    ),
                    title="📤 Export Complete",
                    border_style="green"
                ),
                "rich"
            )
        
        except Exception as e:
            raise CommandError(f"Failed to export logs: {str(e)}")
    
    async def _clear_logs(self) -> CommandResult:
        """Clear in-memory logs"""
        
        old_count = len(self.audit_logger.recent_events)
        self.audit_logger.recent_events.clear()
        
        return CommandResult.success_result(
            Panel(
                Text(
                    f"✅ **In-Memory Logs Cleared**\n\n"
                    f"Cleared {old_count} events from memory.\n\n"
                    f"Note: Persistent logs in {self.audit_logger.log_file} are preserved.\n"
                    f"Use file system tools to manage the log file if needed.",
                    style="yellow"
                ),
                title="🗑️ Logs Cleared",
                border_style="yellow"
            ),
            "rich"
        )
    
    def get_help(self) -> str:
        """Get command help"""
        return """View security audit logs and system events

Usage:
  /logs                       Show logs overview and recent activity
  /logs recent [N]            Show N most recent events (default: 50)
  /logs security              Show security-related events only
  /logs permissions           Show permission checks and denials
  /logs secrets               Show secret detection events
  /logs tools                 Show tool execution logs
  /logs plans                 Show plan execution logs
  /logs errors                Show errors and warnings only
  /logs summary               Show detailed statistics
  /logs export <file> [fmt]   Export logs to file (json/csv)
  /logs clear                 Clear in-memory logs (keeps persistent logs)

Event Types:
  • permission_check      - Access control validations
  • permission_denied     - Blocked access attempts
  • secret_detected       - Sensitive data found in files
  • secret_redacted       - Sensitive data sanitized
  • tool_execution        - Tool run events with results
  • plan_execution        - Plan workflow events
  • approval_required     - Manual approval needed
  • approval_granted      - Manual approval given
  • sandbox_violation     - Security policy violation

Severity Levels:
  • debug       - Debug information
  • info        - General information
  • warning     - Potential issues
  • error       - Errors and failures
  • critical    - Critical security events

Export Formats:
  • json        - JSON Lines format (default)
  • csv         - Comma-separated values

Examples:
  /logs recent 100                    Show last 100 events
  /logs security                      Show security events
  /logs permissions                   Show access control events
  /logs export audit_report.json     Export to JSON file
  /logs export summary.csv csv        Export to CSV file

Log Files:
  • logs/security_audit.log          - Persistent structured logs
  • In-memory buffer                 - Recent events for quick access

Related Commands:
  • /permissions - Manage security rules
  • /sandbox - Control execution isolation
  • /tools - List available tools
  • /status - System status overview

Aliases: /log, /audit"""