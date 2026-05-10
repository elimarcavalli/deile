"""Apply Command - Apply patch files to current directory"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import ACTION_EMOJI, PATCHES_DIR, split_args


class ApplyCommand(DirectCommand):
    """Apply patch files to current directory"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="apply",
            description="Apply patch files to current directory.",
            aliases=["patch-apply"],  # alias canônico mantido por retrocompatibilidade
        )
        super().__init__(config)
        self.patches_dir = PATCHES_DIR
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute apply command"""
        try:
            parts = split_args(context)
            
            if not parts:
                # Show available patches to apply
                return await self._show_applicable_patches()
            
            patch_file = parts[0]
            
            # Parse options
            dry_run = False
            force = False
            backup = True
            target_dir = "."
            
            for i, part in enumerate(parts[1:], 1):
                if part == "--dry-run":
                    dry_run = True
                elif part == "--force":
                    force = True
                elif part == "--no-backup":
                    backup = False
                elif part.startswith("--target="):
                    target_dir = part.split("=", 1)[1]
                elif part == "--target":
                    if i + 1 < len(parts):
                        target_dir = parts[i + 1]
                    else:
                        raise CommandError("--target requires a directory path")
                elif part.startswith("--"):
                    raise CommandError(f"Unknown option: {part}")
            
            return await self._apply_patch(patch_file, target_dir, dry_run, force, backup)
            
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute apply command: {str(e)}")
    
    async def _show_applicable_patches(self) -> CommandResult:
        """Show available patch files that can be applied"""
        
        patch_files = []
        
        # Look in PATCHES directory
        if self.patches_dir.exists():
            patch_files.extend(self.patches_dir.glob("*.patch"))
        
        # Also look in current directory
        patch_files.extend(Path(".").glob("*.patch"))
        
        # Remove duplicates and sort
        patch_files = sorted(set(patch_files), key=lambda f: f.stat().st_mtime, reverse=True)
        
        if not patch_files:
            return CommandResult.success_result(
                Panel(
                    Text("No patch files found.\n\nGenerate patches with '/patch-generate <plan_id>' or place .patch files in the current directory.", 
                         style="yellow"),
                    title="📦 No Patches Available",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Create table of applicable patches
        table = Table(title=f"📦 Available Patches ({len(patch_files)} files)", show_header=True, header_style="bold blue")
        table.add_column("Filename", style="cyan", width=25)
        table.add_column("Location", style="yellow", width=15)
        table.add_column("Size", style="blue", width=10)
        table.add_column("Plan ID", style="green", width=10)
        table.add_column("Action", style="magenta", width=20)
        
        for patch_file in patch_files:
            # Extract info from patch
            patch_info = await self._analyze_patch_file(patch_file)
            
            # Location
            location = "PATCHES/" if patch_file.parent.name == "PATCHES" else "current"
            
            # File size
            size = f"{patch_file.stat().st_size:,}B"
            
            # Action
            action_text = f"/patch-apply {patch_file.name}"
            
            table.add_row(
                patch_file.name,
                location,
                size,
                patch_info.get('plan_id', 'Unknown'),
                action_text
            )
        
        # Add usage instructions
        usage_panel = Panel(
            Text(
                "Usage:\n"
                "• /patch-apply <patch_file>              - Apply patch with preview\n"
                "• /patch-apply <patch_file> --dry-run    - Preview changes without applying\n"
                "• /patch-apply <patch_file> --force      - Apply without confirmation\n"
                "• /patch-apply <patch_file> --no-backup  - Don't create backup files\n"
                "• /patch-apply <patch_file> --target=<dir> - Apply to specific directory\n\n"
                "Backup files (.orig) are created by default for safety.",
                style="dim"
            ),
            title="Usage Instructions",
            border_style="dim"
        )
        
        return CommandResult.success_result(f"{table}\n\n{usage_panel}", "rich")
    
    async def _apply_patch(self, patch_file: str, target_dir: str, 
                          dry_run: bool, force: bool, backup: bool) -> CommandResult:
        """Apply a patch file"""
        
        # Resolve patch file path
        patch_path = Path(patch_file)
        if not patch_path.exists():
            # Try in PATCHES directory
            patch_path = self.patches_dir / patch_file
            if not patch_path.exists():
                raise CommandError(f"Patch file '{patch_file}' not found")
        
        # Validate target directory
        target_path = Path(target_dir)
        if not target_path.exists():
            raise CommandError(f"Target directory '{target_dir}' does not exist")
        
        # Parse patch file
        try:
            patch_data = await self._parse_patch_file(patch_path)
        except Exception as e:
            raise CommandError(f"Failed to parse patch file: {str(e)}")
        
        if not patch_data['file_changes']:
            return CommandResult.success_result(
                Panel(
                    Text(f"Patch file '{patch_file}' contains no applicable changes.", style="yellow"),
                    title="📦 Empty Patch",
                    border_style="yellow"
                ),
                "rich"
            )
        
        # Validate patch can be applied
        validation = await self._validate_patch_application(patch_data, target_path)
        
        if validation['has_conflicts'] and not force:
            return self._format_patch_conflicts(validation, patch_file)

        # Show preview if dry run
        if dry_run:
            return self._format_dry_run_result(patch_data, target_path, validation)

        # Ask for confirmation unless force is used
        if not force:
            # In a real implementation, this would use a proper confirmation UI
            # For now, we'll show what would be confirmed
            return self._format_apply_preview(patch_data, target_path, validation)

        # Apply the patch
        try:
            apply_result = await self._perform_patch_application(patch_data, target_path, backup)
            return self._format_apply_result(apply_result, patch_file, target_dir)
        
        except Exception as e:
            raise CommandError(f"Failed to apply patch: {str(e)}")
    
    async def _analyze_patch_file(self, patch_path: Path) -> dict[str, Any]:
        """Analyze patch file to extract metadata"""
        
        try:
            content = await asyncio.to_thread(patch_path.read_text, encoding='utf-8')

            # Extract plan ID from header
            plan_id_match = re.search(r'^# Plan ID: (.+)$', content, re.MULTILINE)
            plan_id = plan_id_match.group(1) if plan_id_match else "Unknown"
            
            # Count file changes
            file_count = len(re.findall(r'^diff --git|^---.*\t|^File:', content, re.MULTILINE))
            
            # Extract format
            format_match = re.search(r'^# Format: (.+)$', content, re.MULTILINE)
            patch_format = format_match.group(1) if format_match else "unknown"
            
            return {
                'plan_id': plan_id,
                'file_count': file_count,
                'format': patch_format,
                'size': len(content)
            }
            
        except Exception:
            return {
                'plan_id': 'Unknown',
                'file_count': 0,
                'format': 'unknown',
                'size': 0
            }
    
    async def _parse_patch_file(self, patch_path: Path) -> dict[str, Any]:
        """Parse patch file content"""
        
        content = await asyncio.to_thread(patch_path.read_text, encoding='utf-8')

        # Extract metadata from header
        metadata = {}
        plan_id_match = re.search(r'^# Plan ID: (.+)$', content, re.MULTILINE)
        if plan_id_match:
            metadata['plan_id'] = plan_id_match.group(1)
        
        format_match = re.search(r'^# Format: (.+)$', content, re.MULTILINE)
        if format_match:
            metadata['format'] = format_match.group(1)
        
        # Parse file changes based on format
        file_changes = []
        
        if metadata.get('format') == 'git':
            file_changes = self._parse_git_format(content)
        elif metadata.get('format') == 'simple':
            file_changes = self._parse_simple_format(content)
        else:  # unified or unknown
            file_changes = self._parse_unified_format(content)
        
        return {
            'metadata': metadata,
            'file_changes': file_changes,
            'raw_content': content
        }
    
    def _parse_unified_format(self, content: str) -> list[dict[str, Any]]:
        """Parse unified diff format"""
        
        changes = []
        lines = content.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Look for file header
            if line.startswith('---') and i + 1 < len(lines) and lines[i + 1].startswith('+++'):
                old_file = lines[i].replace('--- ', '').split('\t')[0]
                new_file = lines[i + 1].replace('+++ ', '').split('\t')[0]
                
                # Determine action
                if old_file == '/dev/null':
                    action = 'created'
                    file_path = new_file
                elif new_file == '/dev/null':
                    action = 'deleted'
                    file_path = old_file
                else:
                    action = 'modified'
                    file_path = new_file
                
                # Extract content changes (simplified)
                content_lines = []
                j = i + 2
                while j < len(lines) and not lines[j].startswith('---'):
                    if lines[j].startswith('+') and not lines[j].startswith('+++'):
                        content_lines.append(lines[j][1:])  # Remove + prefix
                    elif lines[j].startswith(' '):
                        content_lines.append(lines[j][1:])  # Context line
                    j += 1
                
                changes.append({
                    'path': file_path,
                    'action': action,
                    'new_content': '\n'.join(content_lines) if content_lines else '',
                    'lines_added': len([ln for ln in lines[i:j] if ln.startswith('+') and not ln.startswith('+++')])
                })
                
                i = j
            else:
                i += 1
        
        return changes
    
    def _parse_git_format(self, content: str) -> list[dict[str, Any]]:
        """Parse Git format patches"""
        
        changes = []
        lines = content.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Look for git diff header
            if line.startswith('diff --git'):
                # Extract file paths
                match = re.match(r'diff --git a/(.+) b/(.+)', line)
                if match:
                    old_path, new_path = match.groups()
                    
                    # Look for file mode changes
                    action = 'modified'  # default
                    j = i + 1
                    while j < len(lines) and not lines[j].startswith('diff --git'):
                        if 'new file mode' in lines[j]:
                            action = 'created'
                        elif 'deleted file mode' in lines[j]:
                            action = 'deleted'
                        j += 1
                    
                    # Extract content (simplified)
                    content_lines = []
                    for line in lines[i:j]:
                        if line.startswith('+') and not line.startswith('+++'):
                            content_lines.append(line[1:])
                    
                    changes.append({
                        'path': new_path if action != 'deleted' else old_path,
                        'action': action,
                        'new_content': '\n'.join(content_lines),
                        'lines_added': len(content_lines)
                    })
                    
                    i = j
                else:
                    i += 1
            else:
                i += 1
        
        return changes
    
    def _parse_simple_format(self, content: str) -> list[dict[str, Any]]:
        """Parse simple text format"""
        
        changes = []
        sections = content.split('---')
        
        for section in sections:
            lines = section.strip().split('\n')
            if not lines:
                continue
            
            file_path = None
            action = 'modified'
            content_lines = []
            
            for line in lines:
                if line.startswith('File: '):
                    file_path = line.replace('File: ', '')
                elif line.startswith('Action: '):
                    action = line.replace('Action: ', '')
                elif line == 'Content:':
                    # Following lines are content
                    content_lines = lines[lines.index(line) + 1:]
                    break
            
            if file_path:
                changes.append({
                    'path': file_path,
                    'action': action,
                    'new_content': '\n'.join(content_lines),
                    'lines_added': len(content_lines)
                })
        
        return changes
    
    async def _validate_patch_application(self, patch_data: dict[str, Any], 
                                        target_path: Path) -> dict[str, Any]:
        """Validate that patch can be applied"""
        
        conflicts = []
        warnings = []
        
        for file_change in patch_data['file_changes']:
            file_path = target_path / file_change['path']
            
            if file_change['action'] == 'created':
                if file_path.exists():
                    conflicts.append(f"File already exists: {file_change['path']}")
            
            elif file_change['action'] == 'modified':
                if not file_path.exists():
                    conflicts.append(f"File to modify not found: {file_change['path']}")
                else:
                    # Check if file has been modified since patch was created
                    # This is a simplified check
                    warnings.append(f"File may have been modified: {file_change['path']}")
            
            elif file_change['action'] == 'deleted':
                if not file_path.exists():
                    warnings.append(f"File to delete not found: {file_change['path']}")
        
        return {
            'has_conflicts': len(conflicts) > 0,
            'conflicts': conflicts,
            'warnings': warnings,
            'files_affected': len(patch_data['file_changes'])
        }
    
    @staticmethod
    def _format_patch_conflicts(validation: dict[str, Any], patch_file: str) -> CommandResult:
        """Format patch conflict information (pure helper — no I/O, no self)."""
        
        content_lines = [
            "⚠️ **Patch Conflicts Detected**",
            "",
            f"**Patch File:** {patch_file}",
            f"**Files Affected:** {validation['files_affected']}",
            f"**Conflicts:** {len(validation['conflicts'])}",
            f"**Warnings:** {len(validation['warnings'])}",
            "",
            "**Conflicts:**"
        ]
        
        for conflict in validation['conflicts']:
            content_lines.append(f"  ❌ {conflict}")
        
        if validation['warnings']:
            content_lines.extend([
                "",
                "**Warnings:**"
            ])
            for warning in validation['warnings']:
                content_lines.append(f"  ⚠️ {warning}")
        
        content_lines.extend([
            "",
            "**Resolution:**",
            f"• Use `/patch-apply {patch_file} --force` to apply anyway (risky)",
            f"• Use `/patch-apply {patch_file} --dry-run` to preview changes",
            "• Manually resolve conflicts and try again",
            "• Generate a new patch from the original plan"
        ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="red"),
            title="⚠️ Patch Conflicts",
            border_style="red",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    @staticmethod
    def _format_dry_run_result(patch_data: dict[str, Any],
                               target_path: Path, validation: dict[str, Any]) -> CommandResult:
        """Format dry run preview (pure helper — no I/O, no self)."""
        
        metadata = patch_data.get('metadata', {})
        
        content_lines = [
            "🔍 **Dry Run Preview**",
            "",
            f"**Patch:** {metadata.get('plan_id', 'Unknown')}",
            f"**Target:** {target_path}",
            f"**Files to Change:** {len(patch_data['file_changes'])}",
            ""
        ]
        
        # Show file changes
        if patch_data['file_changes']:
            content_lines.append("**Changes Preview:**")
            
            for file_change in patch_data['file_changes']:
                action_emoji = ACTION_EMOJI.get(file_change['action'], '❓')

                content_lines.append(f"  {action_emoji} {file_change['action'].title()}: {file_change['path']}")
                
                # Show content preview for small changes
                if file_change['lines_added'] <= 5:
                    preview_lines = file_change['new_content'].split('\n')[:3]
                    for preview_line in preview_lines:
                        if preview_line.strip():
                            content_lines.append(f"     + {preview_line[:50]}")
                    if len(file_change['new_content'].split('\n')) > 3:
                        content_lines.append("     + ...")
        
        # Show validation results
        if validation['conflicts']:
            content_lines.extend([
                "",
                "⚠️ **Conflicts:**"
            ])
            for conflict in validation['conflicts']:
                content_lines.append(f"  ❌ {conflict}")
        
        if validation['warnings']:
            content_lines.extend([
                "",
                "⚠️ **Warnings:**"
            ])
            for warning in validation['warnings']:
                content_lines.append(f"  ⚠️ {warning}")
        
        content_lines.extend([
            "",
            "**To Apply:**",
            "• Remove `--dry-run` flag to apply changes",
            "• Use `--force` to ignore conflicts",
            "• Use `--no-backup` to skip backup creation"
        ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="blue"),
            title="🔍 Dry Run Preview",
            border_style="blue",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    @staticmethod
    def _format_apply_preview(patch_data: dict[str, Any],
                              target_path: Path, validation: dict[str, Any]) -> CommandResult:
        """Format apply confirmation preview (pure helper — no I/O, no self)."""
        
        content_lines = [
            "📋 **Ready to Apply Patch**",
            "",
            f"**Target Directory:** {target_path}",
            f"**Files to Change:** {len(patch_data['file_changes'])}",
            ""
        ]
        
        # Show summary of changes
        actions_count = {}
        for file_change in patch_data['file_changes']:
            action = file_change['action']
            actions_count[action] = actions_count.get(action, 0) + 1
        
        content_lines.append("**Summary of Changes:**")
        for action, count in actions_count.items():
            emoji = ACTION_EMOJI.get(action, '❓')
            content_lines.append(f"  {emoji} {action.title()}: {count} files")
        
        content_lines.extend([
            "",
            "**Next Steps:**",
            "• Review the changes above",
            "• Add `--force` flag to apply without this preview",
            "• Use `--dry-run` to see detailed preview",
            "",
            "⚠️ **Note:** Backup files (.orig) will be created automatically"
        ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style="cyan"),
            title="📋 Apply Confirmation",
            border_style="cyan",
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _perform_patch_application(self, patch_data: dict[str, Any], 
                                       target_path: Path, backup: bool) -> dict[str, Any]:
        """Actually apply the patch to files"""
        
        applied_files = []
        backed_up_files = []
        errors = []
        
        for file_change in patch_data['file_changes']:
            resolved = (target_path / file_change['path']).resolve()
            if not resolved.is_relative_to(target_path.resolve()):
                raise CommandError(f"Unsafe path in patch: {file_change['path']}")
            file_path = resolved

            try:
                if file_change['action'] == 'created':
                    # Create new file
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(file_path.write_text, file_change['new_content'], 'utf-8')
                    applied_files.append((file_change['path'], 'created'))

                elif file_change['action'] == 'modified':
                    # Create backup if requested
                    if backup and file_path.exists():
                        backup_path = file_path.with_suffix(file_path.suffix + '.orig')
                        shutil.copy2(file_path, backup_path)
                        backed_up_files.append(str(backup_path))

                    # Apply changes (simplified - just replace content)
                    await asyncio.to_thread(file_path.write_text, file_change['new_content'], 'utf-8')
                    applied_files.append((file_change['path'], 'modified'))
                
                elif file_change['action'] == 'deleted':
                    # Create backup if requested
                    if backup and file_path.exists():
                        backup_path = file_path.with_suffix(file_path.suffix + '.orig')
                        shutil.move(file_path, backup_path)
                        backed_up_files.append(str(backup_path))
                    else:
                        file_path.unlink()
                    applied_files.append((file_change['path'], 'deleted'))
            
            except Exception as e:
                errors.append(f"Failed to apply {file_change['action']} to {file_change['path']}: {str(e)}")
        
        return {
            'applied_files': applied_files,
            'backed_up_files': backed_up_files,
            'errors': errors,
            'success': len(errors) == 0
        }
    
    @staticmethod
    def _format_apply_result(result: dict[str, Any],
                             patch_file: str, target_dir: str) -> CommandResult:
        """Format the final application result (pure helper — no I/O, no self)."""
        
        success = result['success']
        applied_files = result['applied_files']
        backed_up_files = result['backed_up_files']
        errors = result['errors']
        
        if success:
            emoji = "✅"
            title = "Patch Applied Successfully"
            style = "green"
            border_color = "green"
        else:
            emoji = "⚠️"
            title = "Patch Applied with Errors"
            style = "yellow"
            border_color = "yellow"
        
        content_lines = [
            f"{emoji} **{title}**",
            "",
            f"**Patch File:** {patch_file}",
            f"**Target Directory:** {target_dir}",
            f"**Files Changed:** {len(applied_files)}",
            f"**Backup Files:** {len(backed_up_files)}",
            ""
        ]
        
        # Show applied changes
        if applied_files:
            content_lines.append("**Changes Applied:**")
            for file_path, action in applied_files:
                action_emoji = ACTION_EMOJI.get(action, '❓')
                content_lines.append(f"  {action_emoji} {action.title()}: {file_path}")
            content_lines.append("")
        
        # Show backup info
        if backed_up_files:
            content_lines.extend([
                "**Backup Files Created:**"
            ])
            for backup_file in backed_up_files[:5]:  # Show first 5
                content_lines.append(f"  💾 {backup_file}")
            if len(backed_up_files) > 5:
                content_lines.append(f"  ... and {len(backed_up_files) - 5} more backup files")
            content_lines.append("")
        
        # Show errors if any
        if errors:
            content_lines.extend([
                "**Errors:**"
            ])
            for error in errors:
                content_lines.append(f"  ❌ {error}")
            content_lines.append("")
        
        # Next steps
        if success:
            content_lines.extend([
                "**Next Steps:**",
                "• Test your changes to ensure they work correctly",
                "• Remove backup files (.orig) when satisfied",
                "• Use version control to track changes"
            ])
        else:
            content_lines.extend([
                "**Next Steps:**",
                "• Review error messages above",
                "• Check file permissions and paths",
                "• Restore from backup files if needed",
                "• Try applying the patch manually"
            ])
        
        content = "\n".join(content_lines)
        
        result_panel = Panel(
            Text(content, style=style),
            title=f"📦 {title}",
            border_style=border_color,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Apply patch files to current directory

Usage:
  /patch-apply                          Show available patches to apply
  /patch-apply <patch_file>             Apply patch with confirmation
  /patch-apply <patch_file> --dry-run   Preview changes without applying
  /patch-apply <patch_file> --force     Apply without confirmation
  /patch-apply <patch_file> --no-backup Don't create backup files
  /patch-apply <patch_file> --target=<dir> Apply to specific directory

Options:
  --dry-run       Preview changes without applying them
  --force         Apply without confirmation prompts
  --no-backup     Skip creation of .orig backup files
  --target=<dir>  Apply patch to specific target directory

Examples:
  /patch-apply                          List available patches
  /patch-apply plan_abc123.patch        Apply patch with confirmation
  /patch-apply mychanges.patch --dry-run Preview patch application
  /patch-apply bugfix.patch --force     Apply immediately without prompts
  /patch-apply changes.patch --target=./src Apply to specific directory

Patch Sources:
  • PATCHES/ directory - Generated by /patch-generate command
  • Current directory - Manually created or downloaded patches
  • Supports unified diff, Git format, and simple text patches

Safety Features:
  • Backup files (.orig) created by default
  • Conflict detection before application
  • Dry run preview to see changes
  • Force option to override safety checks

File Actions:
  📝 Modified - Existing files updated
  ✨ Created - New files added  
  🗑️ Deleted - Files removed
  💾 Backup - Original files preserved

Related Commands:
  • /patch-generate <plan_id> - Generate patch from plan
  • /diff <plan_id> - Show changes without patching
  • /plan show <plan_id> - View plan details"""