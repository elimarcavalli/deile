"""Patch Command - Generate patch files from plan changes"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ...orchestration.plan_manager import get_plan_manager
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import (analyze_plan_changes_stub, ensure_patches_dir,
                      export_timestamp, file_action_emoji,
                      format_change_summary_lines, list_patch_files,
                      split_args, wrap_command_errors)


class PatchCommand(DirectCommand):
    """Generate patch files from plan execution changes.

    ⚠️  STATUS: STUB — DO NOT USE WITHOUT --i-know-this-is-broken
    ==============================================================

    ``_analyze_plan_changes()`` delegates to
    :func:`analyze_plan_changes_stub`, which returns HARDCODED FAKE plan
    diffs (``src/main.py``, ``config/settings.json``, ``tests/test_main.py``)
    regardless of the plan you pass. The generated patch never reflects
    real plan-execution output, and the per-file diff hunks are not even
    well-formed (the multi-line content is collapsed into a single ``+`` line,
    so the result is unparseable by ``git apply``).

    For agent-driven edits, use the ``edit_file`` tool — it builds
    structured find/replace patches against real file contents.

    This command remains in the registry only for backward compatibility.
    Invoking it without ``--i-know-this-is-broken`` refuses, so a UI test
    or curious user does not produce a confidently-named .patch file full
    of garbage.
    """

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="patch",
            description=(
                "[STUB] Generate plan patch files (currently fake). Requires "
                "--i-know-this-is-broken; otherwise refuses. Prefer the "
                "edit_file tool for real agent edits."
            ),
            aliases=["patch-generate"],
        )
        super().__init__(config)
        self.plan_manager = get_plan_manager()
        # ensure_patches_dir() guarantees creation at init time (was: mkdir inline)
        ensure_patches_dir()

    @wrap_command_errors("patch")
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute patch command"""
        parts = split_args(context)

        if not parts:
            return await self._list_patches()

        plan_id = parts[0]
        output_format = "unified"  # unified, git, simple
        output_path = None
        include_artifacts = False
        broken_opt_in = False

        for i, part in enumerate(parts[1:], 1):
            if part == "--git":
                output_format = "git"
            elif part == "--simple":
                output_format = "simple"
            elif part == "--artifacts":
                include_artifacts = True
            elif part == "--i-know-this-is-broken":
                broken_opt_in = True
            elif part.startswith("--output="):
                output_path = part.split("=", 1)[1]
            elif part == "--output":
                if i + 1 < len(parts):
                    output_path = parts[i + 1]
                else:
                    raise CommandError("--output requires a path")
            elif part.startswith("--"):
                raise CommandError(f"Unknown option: {part}")

        if not broken_opt_in:
            return CommandResult.success_result(
                Panel(
                    Text(
                        "⚠️  /patch-generate is a STUB and refuses to run.\n\n"
                        "The internal analyzer returns hardcoded fake plan diffs "
                        "(src/main.py, config/settings.json, …) regardless of the "
                        "plan you pass — the output is never grounded in real "
                        "plan-execution data.\n\n"
                        "For agent edits use the `edit_file` tool — it builds "
                        "structured find/replace patches against actual file "
                        "contents and applies them atomically.\n\n"
                        "If you understand what you're doing and STILL want to "
                        "produce a fake-data .patch (e.g. for a regression test), "
                        "re-invoke with the explicit flag:\n"
                        f"  /patch-generate {plan_id} --i-know-this-is-broken\n",
                        style="red",
                    ),
                    title="🚫 /patch-generate blocked",
                    border_style="red",
                ),
                "rich",
            )

        return await self._generate_patch(plan_id, output_format, output_path, include_artifacts)
    
    async def _list_patches(self) -> CommandResult:
        """List available patch files"""

        patch_files = list_patch_files()
        
        if not patch_files:
            return CommandResult.success_result(
                Panel(
                    Text("No patch files found.\n\nGenerate patches with '/patch-generate <plan_id>' after executing plans.", 
                         style="yellow"),
                    title="📦 No Patches Available",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create table of available patches
        table = Table(title=f"📦 Available Patches ({len(patch_files)} files)", show_header=True, header_style="bold green")
        table.add_column("Filename", style="cyan")
        table.add_column("Plan ID", style="yellow")
        table.add_column("Created", style="dim")
        table.add_column("Size", style="blue")
        table.add_column("Action", style="magenta")
        
        for patch_file in patch_files:
            # Extract plan ID from filename
            plan_id = "Unknown"
            if "_" in patch_file.stem:
                plan_id = patch_file.stem.split("_")[1]
            
            # File info
            stat = patch_file.stat()
            created = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            size = f"{stat.st_size:,}B"
            
            # Action
            action_text = f"/patch-apply {patch_file.name}"
            
            table.add_row(
                patch_file.name,
                plan_id,
                created,
                size,
                action_text
            )
        
        # Add usage instructions
        usage_panel = Panel(
            Text(
                "Usage:\n"
                "• /patch-generate <plan_id>                  - Generate patch for plan\n"
                "• /patch-generate <plan_id> --git            - Generate Git-format patch\n"
                "• /patch-generate <plan_id> --output=<path>  - Save to specific location\n"
                "• /patch-apply <patch_file>               - Apply patch to current directory\n\n"
                "Patches are saved to PATCHES/ directory by default.",
                style="dim"
            ),
            title="Usage Instructions",
            border_style="dim"
        )
        
        return CommandResult.success_result(f"{table}\n\n{usage_panel}", "rich")
    
    async def _generate_patch(self, plan_id: str, output_format: str, 
                            output_path: Optional[str], include_artifacts: bool) -> CommandResult:
        """Generate patch file for a plan"""
        
        # Validate plan
        plan = await self.plan_manager.load_plan(plan_id)
        if not plan:
            raise CommandError(f"Plan '{plan_id}' not found")
        
        if plan.status not in ['completed', 'failed']:
            raise CommandError(f"Cannot generate patch for plan with status '{plan.status.value}'. Only completed or failed plans can be patched.")
        
        # Analyze plan changes
        changes = await self._analyze_plan_changes(plan_id)
        
        if not changes['has_changes']:
            return CommandResult.success_result(
                Panel(
                    Text(f"Plan '{plan_id}' made no changes that can be patched.\n\nThis plan may have only performed read operations.", 
                         style="yellow"),
                    title="📦 No Patchable Changes",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Generate patch content
        patch_content = await self._create_patch_content(plan, changes, output_format, include_artifacts)
        
        # Determine output path
        if not output_path:
            output_path = ensure_patches_dir() / f"plan_{plan_id}_{export_timestamp()}.patch"
        else:
            output_path = Path(output_path)
        
        # Write patch file
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(output_path.write_text, patch_content, encoding='utf-8')
            return await self._format_patch_result(plan, changes, output_path, output_format, include_artifacts)
        except Exception as e:
            raise CommandError(f"Failed to write patch file: {str(e)}")
    
    async def _analyze_plan_changes(self, plan_id: str) -> Dict[str, Any]:
        """Stub — see analyze_plan_changes_stub for the canonical placeholder."""
        return analyze_plan_changes_stub(plan_id)
    
    async def _create_patch_content(self, plan, changes: Dict[str, Any], 
                                  output_format: str, include_artifacts: bool) -> str:
        """Create patch file content"""
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Header
        lines = [
            f"# Patch generated from Plan: {plan.title}",
            f"# Plan ID: {plan.id}",
            f"# Generated: {timestamp}",
            f"# Format: {output_format}",
            f"# Status: {plan.status.value}",
            ""
        ]
        
        # Summary
        summary = changes['summary']
        lines.extend([
            "# Summary:",
            f"# Files modified: {summary['files_modified']}",
            f"# Files created: {summary['files_created']}",
            f"# Files deleted: {summary['files_deleted']}",
            f"# Lines added: +{summary['lines_added']}",
            f"# Lines removed: -{summary['lines_removed']}",
            ""
        ])
        
        # Generate patches for each file
        for file_change in changes['file_changes']:
            if output_format == "git":
                lines.extend(self._generate_git_patch(file_change))
            elif output_format == "simple":
                lines.extend(self._generate_simple_patch(file_change))
            else:  # unified
                lines.extend(self._generate_unified_patch(file_change))
            
            lines.append("")
        
        # Include artifact information if requested
        if include_artifacts and changes.get('artifacts'):
            lines.extend([
                "# Artifacts generated:",
                "# (These files were created during plan execution)"
            ])
            for artifact in changes['artifacts']:
                lines.append(f"# {artifact}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _generate_unified_patch(self, file_change: Dict[str, Any]) -> List[str]:
        """Generate unified diff format"""
        
        path = file_change['path']
        action = file_change['action']
        
        if action == 'created':
            return [
                "--- /dev/null",
                f"+++ {path}",
                f"@@ -0,0 +1,{file_change['lines_added']} @@",
                f"+{file_change['new_content']}"
            ]
        elif action == 'deleted':
            return [
                f"--- {path}",
                "+++ /dev/null",
                f"@@ -1,{file_change['lines_removed']} +0,0 @@",
                f"-{file_change['old_content']}"
            ]
        else:  # modified
            return [
                f"--- {path}\t(original)",
                f"+++ {path}\t(modified)",
                f"@@ -{file_change['lines_removed']} +{file_change['lines_added']} @@",
                f"-{file_change['old_content']}",
                f"+{file_change['new_content']}"
            ]
    
    def _generate_git_patch(self, file_change: Dict[str, Any]) -> List[str]:
        """Generate Git-format patch"""
        
        path = file_change['path']
        action = file_change['action']
        
        lines = []
        
        if action == 'created':
            lines.extend([
                f"diff --git a/{path} b/{path}",
                "new file mode 100644",
                "index 0000000..abcdef1",
                "--- /dev/null",
                f"+++ b/{path}",
                f"@@ -0,0 +1,{file_change['lines_added']} @@",
                f"+{file_change['new_content']}"
            ])
        elif action == 'deleted':
            lines.extend([
                f"diff --git a/{path} b/{path}",
                "deleted file mode 100644",
                "index abcdef1..0000000",
                f"--- a/{path}",
                "+++ /dev/null",
                f"@@ -1,{file_change['lines_removed']} +0,0 @@",
                f"-{file_change['old_content']}"
            ])
        else:  # modified
            lines.extend([
                f"diff --git a/{path} b/{path}",
                "index abcdef1..1234567 100644",
                f"--- a/{path}",
                f"+++ b/{path}",
                f"@@ -{file_change['lines_removed']} +{file_change['lines_added']} @@",
                f"-{file_change['old_content']}",
                f"+{file_change['new_content']}"
            ])
        
        return lines
    
    def _generate_simple_patch(self, file_change: Dict[str, Any]) -> List[str]:
        """Generate simple patch format"""
        
        path = file_change['path']
        action = file_change['action']
        
        return [
            f"File: {path}",
            f"Action: {action}",
            f"Changes: +{file_change['lines_added']}/-{file_change['lines_removed']}",
            "Content:",
            file_change['new_content'],
            "---"
        ]
    
    async def _format_patch_result(self, plan, changes: Dict[str, Any], 
                                 output_path: Path, output_format: str, include_artifacts: bool) -> CommandResult:
        """Format the patch generation result"""
        
        file_size = output_path.stat().st_size
        summary = changes['summary']
        
        content_lines = [
            "📦 **Patch Generated Successfully**",
            "",
            f"**Plan:** {plan.title}",
            f"**Plan ID:** {plan.id}",
            f"**Output:** {output_path}",
            f"**Format:** {output_format}",
            f"**Size:** {file_size:,} bytes",
            "",
            *format_change_summary_lines(summary, header="**Changes Included:**"),
            "",
        ]
        
        # Show affected files
        if changes['file_changes']:
            content_lines.append("**Files in Patch:**")
            for file_change in changes['file_changes'][:5]:  # Show first 5
                content_lines.append(f"  {file_action_emoji(file_change['action'])} {file_change['path']}")
            
            if len(changes['file_changes']) > 5:
                content_lines.append(f"  ... and {len(changes['file_changes']) - 5} more files")
            
            content_lines.append("")
        
        # Artifacts info
        if include_artifacts and changes.get('artifacts'):
            content_lines.extend([
                "**Artifacts Referenced:**",
                f"  • {len(changes['artifacts'])} artifact files included",
                ""
            ])
        
        # Usage instructions
        content_lines.extend([
            "**Apply Patch:**",
            f"  • `/patch-apply {output_path.name}` - Apply to current directory",
            f"  • `git apply {output_path}` - Apply using Git (if git format)",
            "  • Manual review recommended before applying",
            "",
            "**View Patch:**",
            f"  • View file: `{output_path}`",
            f"  • Preview: `/diff {plan.id} --unified`"
        ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="green"),
            title="📦 Patch Created",
            border_style="green",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Generate patch files from plan execution changes

Usage:
  /patch-generate                          List available patch files
  /patch-generate <plan_id>                Generate unified patch for plan
  /patch-generate <plan_id> --git          Generate Git-format patch
  /patch-generate <plan_id> --simple       Generate simple text patch
  /patch-generate <plan_id> --artifacts    Include artifact references
  /patch-generate <plan_id> --output=<path> Save to specific location

Format Options:
  --git           Generate Git-compatible patch format
  --simple        Generate simple text-based patch
  --artifacts     Include references to generated artifacts
  --output=<path> Specify output file path

Examples:
  /patch-generate                          List all available patches
  /patch-generate abc123                   Generate patch for plan abc123
  /patch-generate abc123 --git             Generate Git patch for plan abc123
  /patch-generate abc123 --output=my.patch Save patch to specific file
  /patch-generate abc123 --artifacts       Include artifact file references

Patch Formats:
  • unified (default) - Standard unified diff format
  • git - Git-compatible format with metadata
  • simple - Human-readable text format

File Locations:
  • Default: PATCHES/plan_<id>_<timestamp>.patch
  • Custom: Specified with --output option

Related Commands:
  • /patch-apply <patch_file> - Apply patch to directory
  • /diff <plan_id> - Show changes without creating patch
  • /plan show <plan_id> - Show plan details"""