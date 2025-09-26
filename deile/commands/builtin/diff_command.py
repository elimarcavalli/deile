"""Diff Command - Show differences and changes from plan execution"""

from typing import Dict, Any, Optional, List
import json
from pathlib import Path
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.syntax import Syntax

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...orchestration.plan_manager import get_plan_manager


class DiffCommand(DirectCommand):
    """Show differences and changes from plan execution"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="diff",
            description="Show differences and changes from plan execution.",
            aliases=["changes", "delta"]
        )
        super().__init__(config)
        self.plan_manager = get_plan_manager()
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute diff command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show recent changes from all plans
                return await self._show_recent_changes()
            
            target = parts[0]
            
            # Parse options
            format_type = "summary"  # summary, detailed, unified
            show_content = False
            
            for part in parts[1:]:
                if part == "--detailed":
                    format_type = "detailed"
                elif part == "--unified":
                    format_type = "unified"
                elif part == "--content":
                    show_content = True
                elif part.startswith("--"):
                    raise CommandError(f"Unknown option: {part}")
            
            # Check if target is a plan ID or file path
            if len(target) == 8 and target.isalnum():  # Looks like plan ID
                return await self._show_plan_diff(target, format_type, show_content)
            else:
                # Treat as file path
                return await self._show_file_diff(target, format_type, show_content)
            
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute diff command: {str(e)}")
    
    async def _show_recent_changes(self) -> CommandResult:
        """Show recent changes from all completed plans"""
        
        # Get completed plans from last 24 hours
        completed_plans = await self.plan_manager.list_plans()
        recent_plans = [p for p in completed_plans if p['status'] in ['completed', 'failed']]
        
        if not recent_plans:
            return CommandResult.success_result(
                Panel(
                    Text("No recent plan executions found.\n\nExecute plans with '/run <plan_id>' to see changes here.", 
                         style="yellow"),
                    title="📊 No Recent Changes",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create summary table
        table = Table(title="📊 Recent Plan Changes", show_header=True, header_style="bold blue")
        table.add_column("Plan ID", style="cyan", width=10)
        table.add_column("Title", style="white", width=25)
        table.add_column("Status", style="green", width=12)
        table.add_column("Changes", style="yellow", width=12)
        table.add_column("Files", style="blue", width=8)
        table.add_column("Duration", style="dim", width=10)
        table.add_column("Action", style="magenta", width=18)
        
        for plan in recent_plans[:10]:  # Show last 10 plans
            # Mock data for changes (in real implementation, would analyze artifacts)
            changes = self._analyze_plan_changes(plan['id'])
            
            # Status emoji
            status_emoji = "✅" if plan['status'] == 'completed' else "❌"
            status_text = f"{status_emoji} {plan['status']}"
            
            # Changes summary
            total_changes = changes.get('files_modified', 0) + changes.get('files_created', 0)
            changes_text = f"{total_changes} changes" if total_changes > 0 else "No changes"
            
            # Files affected
            files_affected = changes.get('files_affected', 0)
            
            # Duration (mock)
            duration = "2m 15s"  # Would be calculated from plan timing
            
            # Action
            action_text = f"/diff {plan['id']}"
            
            table.add_row(
                plan['id'],
                plan['title'][:25] + ("..." if len(plan['title']) > 25 else ""),
                status_text,
                changes_text,
                str(files_affected),
                duration,
                action_text
            )
        
        # Add usage instructions
        usage_panel = Panel(
            Text(
                "Usage:\n"
                "• /diff <plan_id>           - Show changes for specific plan\n"
                "• /diff <file_path>         - Show changes for specific file\n"
                "• /diff <plan_id> --unified - Show unified diff format\n"
                "• /diff <plan_id> --content - Include file content changes\n\n"
                "Use '/patch <plan_id>' to generate patch files.",
                style="dim"
            ),
            title="Usage Instructions",
            border_style="dim"
        )
        
        return CommandResult.success_result(f"{table}\n\n{usage_panel}", "rich")
    
    async def _show_plan_diff(self, plan_id: str, format_type: str, show_content: bool) -> CommandResult:
        """Show differences for a specific plan"""
        
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        # Analyze changes made by this plan
        changes = await self._analyze_plan_changes_detailed(plan_id)
        
        if not changes['has_changes']:
            return CommandResult.success_result(
                Panel(
                    Text(f"Plan '{plan_id}' made no detectable changes.\n\nThis may be because:\n• Plan only read files\n• Plan failed before making changes\n• Changes were outside tracked directories", 
                         style="yellow"),
                    title="📊 No Changes Detected",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create diff display based on format
        if format_type == "summary":
            return await self._format_diff_summary(plan, changes)
        elif format_type == "detailed":
            return await self._format_diff_detailed(plan, changes, show_content)
        elif format_type == "unified":
            return await self._format_diff_unified(plan, changes)
        else:
            raise CommandError(f"Unknown diff format: {format_type}")
    
    async def _show_file_diff(self, file_path: str, format_type: str, show_content: bool) -> CommandResult:
        """Show differences for a specific file"""
        
        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            raise CommandError(f"File '{file_path}' not found")
        
        # Find plans that modified this file
        plans_affecting_file = await self._find_plans_affecting_file(file_path)
        
        if not plans_affecting_file:
            return CommandResult.success_result(
                Panel(
                    Text(f"No recent plan executions modified '{file_path}'.\n\nThis file may have been changed outside of plan execution.", 
                         style="yellow"),
                    title="📊 No Plan Changes Found",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Show file change history
        return await self._format_file_change_history(file_path, plans_affecting_file, format_type, show_content)
    
    def _analyze_plan_changes(self, plan_id: str) -> Dict[str, Any]:
        """Analyze changes made by a plan (simplified version)"""
        
        # Mock implementation - in real version would analyze artifacts
        return {
            'files_modified': 3,
            'files_created': 1,
            'files_deleted': 0,
            'files_affected': 4,
            'lines_added': 45,
            'lines_removed': 12,
            'total_changes': 57
        }
    
    async def _analyze_plan_changes_detailed(self, plan_id: str) -> Dict[str, Any]:
        """Detailed analysis of plan changes"""
        
        # Mock detailed changes analysis
        return {
            'has_changes': True,
            'plan_id': plan_id,
            'summary': {
                'files_modified': 3,
                'files_created': 1,
                'files_deleted': 0,
                'lines_added': 45,
                'lines_removed': 12
            },
            'file_changes': [
                {
                    'path': 'src/main.py',
                    'action': 'modified',
                    'lines_added': 15,
                    'lines_removed': 5,
                    'preview': 'Added error handling and logging'
                },
                {
                    'path': 'config/settings.json',
                    'action': 'modified',
                    'lines_added': 3,
                    'lines_removed': 2,
                    'preview': 'Updated database configuration'
                },
                {
                    'path': 'tests/test_main.py',
                    'action': 'created',
                    'lines_added': 27,
                    'lines_removed': 0,
                    'preview': 'New unit tests for main module'
                }
            ],
            'artifacts_generated': [
                'ARTIFACTS/session_123/bash_output_001.txt',
                'ARTIFACTS/session_123/file_list_002.json'
            ]
        }
    
    async def _find_plans_affecting_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Find plans that affected a specific file"""
        
        # Mock implementation
        return [
            {
                'plan_id': 'abc123',
                'title': 'Code refactoring',
                'timestamp': '2025-09-06T18:30:00',
                'action': 'modified',
                'changes': {'lines_added': 10, 'lines_removed': 3}
            }
        ]
    
    async def _format_diff_summary(self, plan, changes: Dict[str, Any]) -> CommandResult:
        """Format diff as summary"""
        
        summary = changes['summary']
        
        content_lines = [
            f"📊 **Change Summary for Plan: {plan.title}**",
            "",
            f"**Plan ID:** {plan.id}",
            f"**Status:** {plan.status.value}",
            f"**Total Files:** {len(changes['file_changes'])}",
            "",
            f"**Overall Changes:**",
            f"  • Files Modified: {summary['files_modified']} 📝",
            f"  • Files Created: {summary['files_created']} ✨",
            f"  • Files Deleted: {summary['files_deleted']} 🗑️",
            f"  • Lines Added: +{summary['lines_added']} 🟢",
            f"  • Lines Removed: -{summary['lines_removed']} 🔴",
            "",
            "**File Changes:**"
        ]
        
        for file_change in changes['file_changes']:
            action_emoji = {
                'modified': '📝',
                'created': '✨',
                'deleted': '🗑️'
            }.get(file_change['action'], '❓')
            
            added = file_change.get('lines_added', 0)
            removed = file_change.get('lines_removed', 0)
            
            content_lines.append(
                f"  {action_emoji} **{file_change['path']}** (+{added}/-{removed})"
            )
            content_lines.append(f"     {file_change.get('preview', 'No preview')}")
        
        # Show artifacts if any
        if changes.get('artifacts_generated'):
            content_lines.extend([
                "",
                "**Artifacts Generated:**"
            ])
            for artifact in changes['artifacts_generated']:
                content_lines.append(f"  📄 {artifact}")
        
        content_lines.extend([
            "",
            "**View Details:**",
            f"  • `/diff {plan.id} --detailed` - Detailed file-by-file changes",
            f"  • `/diff {plan.id} --unified` - Unified diff format",
            f"  • `/patch {plan.id}` - Generate patch files"
        ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="blue"),
            title="📊 Plan Changes",
            border_style="blue",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _format_diff_detailed(self, plan, changes: Dict[str, Any], show_content: bool) -> CommandResult:
        """Format detailed diff"""
        
        # Create detailed file-by-file breakdown
        content_lines = [
            f"📋 **Detailed Changes for Plan: {plan.title}**",
            "",
            f"**Plan ID:** {plan.id}",
            ""
        ]
        
        for i, file_change in enumerate(changes['file_changes'], 1):
            action_emoji = {
                'modified': '📝',
                'created': '✨', 
                'deleted': '🗑️'
            }.get(file_change['action'], '❓')
            
            content_lines.extend([
                f"## {i}. {action_emoji} {file_change['path']}",
                "",
                f"**Action:** {file_change['action'].title()}",
                f"**Changes:** +{file_change.get('lines_added', 0)}/-{file_change.get('lines_removed', 0)}",
                f"**Summary:** {file_change.get('preview', 'No preview')}",
                ""
            ])
            
            # Show file content preview if requested
            if show_content:
                # Mock content diff
                content_lines.extend([
                    "**Content Changes:**",
                    "```diff",
                    "- old_function_call(param1, param2)",
                    "+ new_improved_call(param1, param2, error_handler=True)",
                    "+ # Added error handling for better reliability",
                    "```",
                    ""
                ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="white"),
            title="📋 Detailed Changes",
            border_style="green",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _format_diff_unified(self, plan, changes: Dict[str, Any]) -> CommandResult:
        """Format as unified diff"""
        
        # Mock unified diff output
        diff_content = f"""--- Plan: {plan.title}
+++ Changes Applied
@@ Plan ID: {plan.id}

Files changed: {len(changes['file_changes'])}
Total changes: +{changes['summary']['lines_added']}/-{changes['summary']['lines_removed']}

"""
        
        for file_change in changes['file_changes']:
            diff_content += f"""
--- {file_change['path']}\t(before)
+++ {file_change['path']}\t(after)
@@ -{file_change.get('lines_removed', 0)} +{file_change.get('lines_added', 0)} @@
 {file_change.get('preview', 'No preview available')}
"""
        
        # Display as syntax highlighted diff
        diff_syntax = Syntax(diff_content, "diff", theme="github-dark", line_numbers=True)
        
        return CommandResult.success_result(diff_syntax, "rich")
    
    async def _format_file_change_history(self, file_path: str, plans: List[Dict[str, Any]], 
                                        format_type: str, show_content: bool) -> CommandResult:
        """Format file change history"""
        
        content_lines = [
            f"📜 **Change History for: {file_path}**",
            "",
            f"**Plans that modified this file: {len(plans)}**",
            ""
        ]
        
        for i, plan_info in enumerate(plans, 1):
            changes = plan_info['changes']
            
            content_lines.extend([
                f"## {i}. Plan {plan_info['plan_id']} - {plan_info['title']}",
                f"**Timestamp:** {plan_info['timestamp'][:19]}",
                f"**Action:** {plan_info['action'].title()}",
                f"**Changes:** +{changes.get('lines_added', 0)}/-{changes.get('lines_removed', 0)}",
                ""
            ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="cyan"),
            title="📜 File History",
            border_style="cyan",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Show differences and changes from plan execution

Usage:
  /diff                           Show recent changes from all plans
  /diff <plan_id>                 Show changes for specific plan
  /diff <file_path>               Show change history for file
  /diff <plan_id> --detailed      Show detailed file-by-file changes
  /diff <plan_id> --unified       Show unified diff format
  /diff <plan_id> --content       Include file content changes

Format Options:
  --detailed      Show detailed breakdown by file
  --unified       Show standard unified diff format
  --content       Include actual file content changes

Examples:
  /diff                           List recent plan changes
  /diff abc123                    Show changes for plan abc123
  /diff src/main.py               Show history of changes to main.py
  /diff abc123 --unified          Show plan changes in diff format
  /diff abc123 --detailed --content  Show detailed changes with content

Change Types:
  📝 Modified files - Files that were changed
  ✨ Created files - New files added
  🗑️ Deleted files - Files removed
  📄 Artifacts - Generated output files

Related Commands:
  • /patch <plan_id> - Generate patch files for changes
  • /apply <patch_file> - Apply patch to current directory
  • /plan show <plan_id> - Show plan execution details

Aliases: /changes, /delta"""