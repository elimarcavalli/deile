"""Export Command - Export conversation, artifacts and session data"""

from typing import Dict, Any, Optional
import json
import os
from datetime import datetime
from pathlib import Path
from rich.panel import Panel
from rich.text import Text
from rich.progress import track

from ..base import DirectCommand
from ...core.exceptions import CommandError


class ExportCommand(DirectCommand):
    """Export conversation history, artifacts, plans and session data in various formats"""
    
    def __init__(self):
        super().__init__(
            name="export",
            description="Export conversation history, artifacts, plans and session data in various formats.",
            aliases=["save", "backup"]
        )
    
    def execute(self, 
               args: str = "",
               context: Optional[Dict[str, Any]] = None) -> Any:
        """Execute export command"""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            format_type = "md"  # default
            export_path = None
            include_artifacts = True
            include_plans = True
            include_session = True
            
            i = 0
            while i < len(parts):
                if parts[i] in ["--format", "-f"]:
                    if i + 1 < len(parts):
                        format_type = parts[i + 1]
                        i += 2
                    else:
                        raise CommandError("--format requires a value (txt, md, json, zip)")
                elif parts[i] in ["--path", "-p"]:
                    if i + 1 < len(parts):
                        export_path = parts[i + 1]
                        i += 2
                    else:
                        raise CommandError("--path requires a directory path")
                elif parts[i] == "--no-artifacts":
                    include_artifacts = False
                    i += 1
                elif parts[i] == "--no-plans":
                    include_plans = False
                    i += 1
                elif parts[i] == "--no-session":
                    include_session = False
                    i += 1
                elif parts[i].startswith("--"):
                    raise CommandError(f"Unknown option: {parts[i]}")
                else:
                    # Positional argument - format or path
                    if format_type == "md" and parts[i] in ["txt", "md", "json", "zip"]:
                        format_type = parts[i]
                    else:
                        export_path = parts[i]
                    i += 1
            
            if format_type not in ["txt", "md", "json", "zip"]:
                raise CommandError("Format must be one of: txt, md, json, zip")
            
            # Set default path if not provided
            if not export_path:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                export_path = f"./EXPORTS/deile_export_{timestamp}"
            
            # Perform export
            return self._perform_export(
                format_type, export_path, include_artifacts, 
                include_plans, include_session, context
            )
            
        except Exception as e:
            raise CommandError(f"Failed to export data: {str(e)}")
    
    def _perform_export(self, format_type: str, export_path: str,
                       include_artifacts: bool, include_plans: bool,
                       include_session: bool, context: Optional[Dict[str, Any]]) -> Panel:
        """Perform the actual export operation"""
        
        # Get export data
        export_data = self._get_export_data(context, include_artifacts, 
                                          include_plans, include_session)
        
        # Create export directory
        export_dir = Path(export_path)
        export_dir.mkdir(parents=True, exist_ok=True)
        
        exported_files = []
        
        if format_type == "zip":
            # Create comprehensive zip export
            zip_path = self._create_zip_export(export_data, export_dir)
            exported_files.append(str(zip_path))
        else:
            # Create individual files
            exported_files = self._create_individual_exports(
                export_data, export_dir, format_type
            )
        
        # Generate summary
        return self._create_export_summary(exported_files, export_data, format_type)
    
    def _get_export_data(self, context: Optional[Dict[str, Any]], 
                        include_artifacts: bool, include_plans: bool,
                        include_session: bool) -> Dict[str, Any]:
        """Get data to export (mock implementation)"""
        
        data = {
            "export_metadata": {
                "timestamp": datetime.now().isoformat(),
                "deile_version": "4.0.0",
                "export_id": f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "format_version": "1.0"
            },
            "conversation": {
                "session_id": "session_20250906_184500",
                "started": "2025-09-06T15:30:00",
                "total_messages": 23,
                "messages": [
                    {
                        "id": 1,
                        "timestamp": "2025-09-06T15:30:00",
                        "role": "user",
                        "content": "Analyze the current architecture and implement improvements",
                        "tokens": 12
                    },
                    {
                        "id": 2,
                        "timestamp": "2025-09-06T15:30:15",
                        "role": "assistant", 
                        "content": "I'll analyze the architecture and implement the requested improvements...",
                        "tokens": 156,
                        "tool_calls": [
                            {"tool": "read_file", "params": {"path": "docs/2.md"}, "result": "success"}
                        ]
                    },
                    # More messages would be here in real implementation
                ],
                "total_tokens": {
                    "prompt": 12500,
                    "completion": 3725,
                    "total": 16225
                }
            }
        }
        
        if include_session:
            data["session_info"] = {
                "model": "gemini-2.5-pro",
                "temperature": 0.7,
                "system_instructions": "You are DEILE, an AI assistant specialized in software development...",
                "persona": {
                    "active": True,
                    "name": "Developer Assistant",
                    "description": "Expert in Python, software architecture, and best practices"
                },
                "memory": {
                    "short_term_entries": 15,
                    "long_term_entries": 45,
                    "total_memory_tokens": 4700
                }
            }
        
        if include_artifacts:
            data["artifacts"] = {
                "total_artifacts": 8,
                "artifacts": [
                    {
                        "id": "artifact_001",
                        "name": "bash_execute_output.txt",
                        "tool": "bash_execute",
                        "timestamp": "2025-09-06T16:45:00",
                        "size": 1024,
                        "path": "ARTIFACTS/session_20250906_184500/artifact_001.txt"
                    },
                    {
                        "id": "artifact_002", 
                        "name": "file_list_output.json",
                        "tool": "list_files",
                        "timestamp": "2025-09-06T17:15:00", 
                        "size": 512,
                        "path": "ARTIFACTS/session_20250906_184500/artifact_002.json"
                    }
                ]
            }
        
        if include_plans:
            data["plans"] = {
                "total_plans": 3,
                "plans": [
                    {
                        "id": "plan_001",
                        "name": "Architecture Improvements",
                        "created": "2025-09-06T15:45:00",
                        "status": "completed",
                        "steps": 8,
                        "path": "PLANS/plan_001.json"
                    }
                ]
            }
        
        return data
    
    def _create_individual_exports(self, data: Dict[str, Any], 
                                 export_dir: Path, format_type: str) -> list:
        """Create individual export files"""
        
        exported_files = []
        
        if format_type == "json":
            # Single JSON file with all data
            json_file = export_dir / "deile_export_complete.json"
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=str)
            exported_files.append(str(json_file))
        
        elif format_type in ["txt", "md"]:
            # Create separate readable files
            
            # Conversation export
            conv_file = export_dir / f"conversation.{format_type}"
            self._write_conversation_file(data.get("conversation", {}), conv_file, format_type)
            exported_files.append(str(conv_file))
            
            # Session info
            if "session_info" in data:
                session_file = export_dir / f"session_info.{format_type}"
                self._write_session_file(data["session_info"], session_file, format_type)
                exported_files.append(str(session_file))
            
            # Artifacts manifest
            if "artifacts" in data:
                artifacts_file = export_dir / f"artifacts_manifest.{format_type}"
                self._write_artifacts_file(data["artifacts"], artifacts_file, format_type)
                exported_files.append(str(artifacts_file))
            
            # Plans manifest
            if "plans" in data:
                plans_file = export_dir / f"plans_manifest.{format_type}"
                self._write_plans_file(data["plans"], plans_file, format_type)
                exported_files.append(str(plans_file))
        
        return exported_files
    
    def _create_zip_export(self, data: Dict[str, Any], export_dir: Path) -> Path:
        """Create comprehensive zip export"""
        import zipfile
        
        zip_path = export_dir / "deile_complete_export.zip"
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Add JSON data
            zipf.writestr("data/complete_export.json", 
                         json.dumps(data, indent=2, default=str))
            
            # Add readable formats
            conv_content = self._format_conversation_content(data.get("conversation", {}), "md")
            zipf.writestr("conversation.md", conv_content)
            
            if "session_info" in data:
                session_content = self._format_session_content(data["session_info"], "md")
                zipf.writestr("session_info.md", session_content)
            
            # Add manifest
            manifest = self._create_export_manifest(data)
            zipf.writestr("MANIFEST.json", json.dumps(manifest, indent=2, default=str))
        
        return zip_path
    
    def _write_conversation_file(self, conv_data: Dict[str, Any], 
                               file_path: Path, format_type: str):
        """Write conversation to file"""
        content = self._format_conversation_content(conv_data, format_type)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    
    def _format_conversation_content(self, conv_data: Dict[str, Any], format_type: str) -> str:
        """Format conversation content"""
        if format_type == "md":
            lines = [
                "# DEILE Conversation Export",
                "",
                f"**Session ID:** {conv_data.get('session_id', 'Unknown')}",
                f"**Started:** {conv_data.get('started', 'Unknown')}",
                f"**Total Messages:** {conv_data.get('total_messages', 0)}",
                f"**Total Tokens:** {conv_data.get('total_tokens', {}).get('total', 0):,}",
                "",
                "## Messages",
                ""
            ]
        else:  # txt
            lines = [
                "DEILE CONVERSATION EXPORT",
                "=" * 50,
                "",
                f"Session ID: {conv_data.get('session_id', 'Unknown')}",
                f"Started: {conv_data.get('started', 'Unknown')}",
                f"Total Messages: {conv_data.get('total_messages', 0)}",
                f"Total Tokens: {conv_data.get('total_tokens', {}).get('total', 0):,}",
                "",
                "MESSAGES:",
                "-" * 20,
                ""
            ]
        
        # Add messages
        for msg in conv_data.get("messages", []):
            if format_type == "md":
                lines.extend([
                    f"### Message {msg.get('id', '')} - {msg.get('role', '').title()}",
                    f"**Time:** {msg.get('timestamp', '')}",
                    f"**Tokens:** {msg.get('tokens', 0)}",
                    "",
                    msg.get('content', ''),
                    ""
                ])
            else:  # txt
                lines.extend([
                    f"[{msg.get('timestamp', '')}] {msg.get('role', '').upper()}:",
                    msg.get('content', ''),
                    f"(Tokens: {msg.get('tokens', 0)})",
                    "",
                ])
        
        return "\n".join(lines)
    
    def _write_session_file(self, session_data: Dict[str, Any], 
                          file_path: Path, format_type: str):
        """Write session info to file"""
        content = self._format_session_content(session_data, format_type)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    
    def _format_session_content(self, session_data: Dict[str, Any], format_type: str) -> str:
        """Format session content"""
        if format_type == "md":
            return f"""# Session Information

## Model Configuration
- **Model:** {session_data.get('model', 'Unknown')}
- **Temperature:** {session_data.get('temperature', 0.7)}

## System Instructions
```
{session_data.get('system_instructions', 'No system instructions')}
```

## Persona
- **Active:** {session_data.get('persona', {}).get('active', False)}
- **Name:** {session_data.get('persona', {}).get('name', 'None')}
- **Description:** {session_data.get('persona', {}).get('description', 'None')}

## Memory Statistics
- **Short-term entries:** {session_data.get('memory', {}).get('short_term_entries', 0)}
- **Long-term entries:** {session_data.get('memory', {}).get('long_term_entries', 0)}
- **Total memory tokens:** {session_data.get('memory', {}).get('total_memory_tokens', 0):,}
"""
        else:  # txt
            return f"""SESSION INFORMATION
==================

Model: {session_data.get('model', 'Unknown')}
Temperature: {session_data.get('temperature', 0.7)}

System Instructions:
{session_data.get('system_instructions', 'No system instructions')}

Persona:
- Active: {session_data.get('persona', {}).get('active', False)}
- Name: {session_data.get('persona', {}).get('name', 'None')}
- Description: {session_data.get('persona', {}).get('description', 'None')}

Memory Statistics:
- Short-term entries: {session_data.get('memory', {}).get('short_term_entries', 0)}
- Long-term entries: {session_data.get('memory', {}).get('long_term_entries', 0)}
- Total memory tokens: {session_data.get('memory', {}).get('total_memory_tokens', 0):,}
"""
    
    def _write_artifacts_file(self, artifacts_data: Dict[str, Any], 
                            file_path: Path, format_type: str):
        """Write artifacts manifest to file"""
        content = "# Artifacts Manifest\n\n" if format_type == "md" else "ARTIFACTS MANIFEST\n=================\n\n"
        
        content += f"Total artifacts: {artifacts_data.get('total_artifacts', 0)}\n\n"
        
        for artifact in artifacts_data.get('artifacts', []):
            if format_type == "md":
                content += f"""## {artifact.get('name', 'Unknown')}
- **ID:** {artifact.get('id', '')}
- **Tool:** {artifact.get('tool', '')}
- **Created:** {artifact.get('timestamp', '')}
- **Size:** {artifact.get('size', 0)} bytes
- **Path:** {artifact.get('path', '')}

"""
            else:
                content += f"""Artifact: {artifact.get('name', 'Unknown')}
  ID: {artifact.get('id', '')}
  Tool: {artifact.get('tool', '')}
  Created: {artifact.get('timestamp', '')}
  Size: {artifact.get('size', 0)} bytes
  Path: {artifact.get('path', '')}

"""
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    
    def _write_plans_file(self, plans_data: Dict[str, Any], 
                         file_path: Path, format_type: str):
        """Write plans manifest to file"""
        content = "# Plans Manifest\n\n" if format_type == "md" else "PLANS MANIFEST\n==============\n\n"
        
        content += f"Total plans: {plans_data.get('total_plans', 0)}\n\n"
        
        for plan in plans_data.get('plans', []):
            if format_type == "md":
                content += f"""## {plan.get('name', 'Unknown')}
- **ID:** {plan.get('id', '')}
- **Created:** {plan.get('created', '')}
- **Status:** {plan.get('status', '')}
- **Steps:** {plan.get('steps', 0)}
- **Path:** {plan.get('path', '')}

"""
            else:
                content += f"""Plan: {plan.get('name', 'Unknown')}
  ID: {plan.get('id', '')}
  Created: {plan.get('created', '')}
  Status: {plan.get('status', '')}
  Steps: {plan.get('steps', 0)}
  Path: {plan.get('path', '')}

"""
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    
    def _create_export_manifest(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create export manifest"""
        return {
            "export_info": data.get("export_metadata", {}),
            "contents": {
                "conversation": "conversation.md",
                "session_info": "session_info.md" if "session_info" in data else None,
                "artifacts": "artifacts_manifest.md" if "artifacts" in data else None,
                "plans": "plans_manifest.md" if "plans" in data else None,
                "raw_data": "data/complete_export.json"
            },
            "statistics": {
                "total_messages": data.get("conversation", {}).get("total_messages", 0),
                "total_tokens": data.get("conversation", {}).get("total_tokens", {}).get("total", 0),
                "total_artifacts": data.get("artifacts", {}).get("total_artifacts", 0),
                "total_plans": data.get("plans", {}).get("total_plans", 0)
            }
        }
    
    def _create_export_summary(self, exported_files: list, 
                             export_data: Dict[str, Any], format_type: str) -> Panel:
        """Create export summary panel"""
        
        metadata = export_data.get("export_metadata", {})
        conv_data = export_data.get("conversation", {})
        
        content_lines = [
            f"✅ **Export Completed Successfully**",
            "",
            f"📊 **Export Statistics**:",
            f"  • Messages: {conv_data.get('total_messages', 0)}",
            f"  • Total Tokens: {conv_data.get('total_tokens', {}).get('total', 0):,}",
            f"  • Artifacts: {export_data.get('artifacts', {}).get('total_artifacts', 0)}",
            f"  • Plans: {export_data.get('plans', {}).get('total_plans', 0)}",
            "",
            f"📁 **Exported Files ({len(exported_files)}):**"
        ]
        
        for file_path in exported_files:
            file_name = Path(file_path).name
            try:
                file_size = Path(file_path).stat().st_size
                content_lines.append(f"  • {file_name} ({file_size:,} bytes)")
            except:
                content_lines.append(f"  • {file_name}")
        
        content_lines.extend([
            "",
            f"🎯 **Export Details**:",
            f"  • Format: {format_type.upper()}",
            f"  • Export ID: {metadata.get('export_id', 'Unknown')}",
            f"  • Timestamp: {metadata.get('timestamp', 'Unknown')[:19]}",
            f"  • Location: {Path(exported_files[0]).parent if exported_files else 'Unknown'}"
        ])
        
        content = "\n".join(content_lines)
        
        return Panel(
            Text(content, style="green"),
            title="📤 Export Complete",
            border_style="green",
            padding=(1, 2)
        )
    
    def get_help(self) -> str:
        """Get command help"""
        return """Export conversation history, artifacts, plans and session data

Usage:
  /export [format] [options]

Formats:
  txt      Export as plain text files
  md       Export as Markdown files (default) 
  json     Export as JSON file
  zip      Export as comprehensive zip archive

Options:
  --path PATH, -p PATH     Export directory path
  --no-artifacts           Exclude artifacts from export
  --no-plans               Exclude plans from export  
  --no-session             Exclude session info from export
  --format FORMAT, -f      Specify export format

Examples:
  /export                           Export as Markdown to default location
  /export zip                       Export as comprehensive zip
  /export json --path ./backups     Export JSON to custom path
  /export md --no-artifacts         Export without artifacts
  
Default path: ./EXPORTS/deile_export_TIMESTAMP/

Aliases: /save, /backup"""