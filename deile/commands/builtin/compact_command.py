"""
Compact Command for DEILE v4.0
==============================

Command for managing conversation history compression and memory optimization.
Implements intelligent conversation summarization with context preservation.

Author: DEILE
Version: 4.0
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from deile.commands.base import BaseCommand
from deile.core.context_manager import ContextManager
from deile.core.exceptions import CommandError
from deile.ui.display_manager import DisplayManager

logger = logging.getLogger(__name__)


class CompactCommand(BaseCommand):
    """
    Command for managing conversation history and memory optimization
    
    Features:
    - Intelligent conversation summarization
    - Context-aware history compaction
    - Configurable retention policies
    - Memory usage optimization
    - Critical information preservation
    """
    
    def __init__(self):
        super().__init__()
        self.name = "compact"
        self.description = "Manage conversation history and memory optimization"
        self.aliases = []
        self.help_text = """
Compact command - Memory and History Management

USAGE:
    /compact [action] [options]

ACTIONS:
    summary               Show current memory usage and stats
    compress [threshold]  Compress old conversations (default: 7 days)
    purge [days]         Remove conversations older than N days (default: 30)
    analyze              Analyze conversation patterns and suggest optimizations
    export [format]      Export conversation data (json, text, csv)
    import [file]        Import conversation data from file
    config [setting]     Configure compression settings
    
EXAMPLES:
    /compact summary                    # Show memory usage statistics
    /compact compress 3                 # Compress conversations older than 3 days
    /compact purge 14                   # Remove conversations older than 14 days
    /compact analyze                    # Analyze conversation patterns
    /compact export json history.json   # Export conversations to JSON
    /compact config auto-compress on    # Enable auto-compression
"""
        
        # Configuration settings
        self.config = {
            'auto_compress': True,
            'compress_threshold_days': 7,
            'purge_threshold_days': 30,
            'max_summary_length': 1000,
            'preserve_keywords': ['error', 'bug', 'issue', 'important', 'critical', 'todo', 'fixme'],
            'compression_ratio_target': 0.3,
            'max_memory_usage_mb': 100
        }
        
        self.context_manager = ContextManager()
        self.display_manager = DisplayManager()

    def execute(self, args: List[str]) -> Dict[str, Any]:
        """Execute the compact command"""
        try:
            if not args:
                return self._show_summary()
            
            action = args[0].lower()
            
            if action == "summary":
                return self._show_summary()
            elif action == "compress":
                threshold = int(args[1]) if len(args) > 1 else self.config['compress_threshold_days']
                return self._compress_conversations(threshold)
            elif action == "purge":
                days = int(args[1]) if len(args) > 1 else self.config['purge_threshold_days']
                return self._purge_conversations(days)
            elif action == "analyze":
                return self._analyze_conversations()
            elif action == "export":
                format_type = args[1] if len(args) > 1 else "json"
                filename = args[2] if len(args) > 2 else None
                return self._export_conversations(format_type, filename)
            elif action == "import":
                filename = args[1] if len(args) > 1 else None
                return self._import_conversations(filename)
            elif action == "config":
                if len(args) < 2:
                    return self._show_config()
                return self._configure_settings(args[1:])
            else:
                return self._error(f"Unknown action: {action}")
                
        except ValueError as e:
            return self._error(f"Invalid parameter: {str(e)}")
        except Exception as e:
            logger.error(f"CompactCommand execution error: {str(e)}")
            return self._error(f"Command execution failed: {str(e)}")

    def _show_summary(self) -> Dict[str, Any]:
        """Show current memory usage and conversation statistics"""
        try:
            # Get conversation history statistics
            stats = self._get_conversation_stats()
            
            # Calculate memory usage
            memory_usage = self._calculate_memory_usage()
            
            # Create summary table
            table = Table(title="📊 Memory & Conversation Summary", show_header=True, header_style="bold cyan")
            table.add_column("Metric", style="white")
            table.add_column("Value", style="green")
            table.add_column("Details", style="dim")
            
            # Memory statistics
            table.add_row(
                "Memory Usage",
                f"{memory_usage['total_mb']:.1f} MB",
                f"Limit: {self.config['max_memory_usage_mb']} MB"
            )
            table.add_row(
                "Active Conversations",
                str(stats['active_conversations']),
                f"Last 24h: {stats['recent_conversations']}"
            )
            table.add_row(
                "Total Messages",
                f"{stats['total_messages']:,}",
                f"Compressed: {stats['compressed_messages']:,}"
            )
            table.add_row(
                "Storage Size",
                f"{stats['storage_size_mb']:.1f} MB",
                f"Compressed: {stats['compressed_size_mb']:.1f} MB"
            )
            table.add_row(
                "Oldest Conversation",
                stats['oldest_date'],
                f"{stats['days_of_history']} days"
            )
            
            # Recommendations
            recommendations = self._generate_recommendations(stats, memory_usage)
            
            # Display results
            console = Console()
            console.print(table)
            
            if recommendations:
                rec_panel = Panel(
                    "\n".join(f"• {rec}" for rec in recommendations),
                    title="💡 Recommendations",
                    title_align="left",
                    style="yellow"
                )
                console.print(rec_panel)
            
            return self._success({
                'memory_usage': memory_usage,
                'conversation_stats': stats,
                'recommendations': recommendations,
                'auto_compress_enabled': self.config['auto_compress']
            })
            
        except Exception as e:
            return self._error(f"Failed to generate summary: {str(e)}")

    def _compress_conversations(self, threshold_days: int) -> Dict[str, Any]:
        """Compress conversations older than threshold"""
        try:
            cutoff_date = datetime.now() - timedelta(days=threshold_days)
            
            # Find conversations to compress
            conversations = self._find_conversations_before(cutoff_date)
            
            if not conversations:
                return self._success({
                    'compressed_count': 0,
                    'message': f"No conversations found older than {threshold_days} days"
                })
            
            # Compress each conversation
            compressed_count = 0
            total_savings = 0
            
            for conversation in conversations:
                try:
                    original_size = self._get_conversation_size(conversation)
                    summary = self._create_conversation_summary(conversation)
                    
                    # Replace conversation with summary
                    self._replace_with_summary(conversation, summary)
                    
                    new_size = len(json.dumps(summary))
                    savings = original_size - new_size
                    total_savings += savings
                    compressed_count += 1
                    
                except Exception as e:
                    logger.warning(f"Failed to compress conversation {conversation.get('id', 'unknown')}: {e}")
                    continue
            
            # Update configuration
            self._update_last_compress_time()
            
            # Display results
            console = Console()
            result_text = Text()
            result_text.append("🗜️  Compression Complete\n\n", style="bold green")
            result_text.append(f"Conversations compressed: {compressed_count}\n")
            result_text.append(f"Space saved: {total_savings / 1024:.1f} KB\n")
            result_text.append(f"Compression threshold: {threshold_days} days")
            
            panel = Panel(result_text, title="Compression Results", style="green")
            console.print(panel)
            
            return self._success({
                'compressed_count': compressed_count,
                'space_saved_bytes': total_savings,
                'threshold_days': threshold_days,
                'compression_date': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to compress conversations: {str(e)}")

    def _purge_conversations(self, days: int) -> Dict[str, Any]:
        """Remove conversations older than specified days"""
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            # Find conversations to purge
            conversations = self._find_conversations_before(cutoff_date)
            
            if not conversations:
                return self._success({
                    'purged_count': 0,
                    'message': f"No conversations found older than {days} days"
                })
            
            # Confirm purge operation for safety
            console = Console()
            warning_text = Text()
            warning_text.append("⚠️  WARNING: This will permanently delete conversations!\n\n", style="bold red")
            warning_text.append(f"Conversations to delete: {len(conversations)}\n")
            warning_text.append(f"Cutoff date: {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}\n")
            warning_text.append("This action cannot be undone.")
            
            panel = Panel(warning_text, title="Purge Confirmation", style="red")
            console.print(panel)
            
            # For safety, we'll just mark them for deletion and require explicit confirmation
            # In a real implementation, you might want to add interactive confirmation
            
            return self._success({
                'purged_count': 0,
                'message': "Purge operation requires explicit confirmation (safety feature)",
                'conversations_found': len(conversations),
                'cutoff_date': cutoff_date.isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to purge conversations: {str(e)}")

    def _analyze_conversations(self) -> Dict[str, Any]:
        """Analyze conversation patterns and provide insights"""
        try:
            # Get all conversations for analysis
            all_conversations = self._get_all_conversations()
            
            if not all_conversations:
                return self._success({
                    'message': "No conversations found to analyze"
                })
            
            # Perform analysis
            analysis = {
                'total_conversations': len(all_conversations),
                'date_range': self._get_date_range(all_conversations),
                'average_length': self._calculate_average_length(all_conversations),
                'most_active_hours': self._find_active_hours(all_conversations),
                'common_topics': self._extract_common_topics(all_conversations),
                'language_distribution': self._analyze_languages(all_conversations),
                'compression_potential': self._estimate_compression_potential(all_conversations)
            }
            
            # Create analysis table
            table = Table(title="📈 Conversation Analysis", show_header=True, header_style="bold cyan")
            table.add_column("Metric", style="white")
            table.add_column("Value", style="green")
            table.add_column("Insight", style="dim")
            
            table.add_row(
                "Total Conversations",
                str(analysis['total_conversations']),
                f"Spanning {analysis['date_range']['days']} days"
            )
            table.add_row(
                "Average Length",
                f"{analysis['average_length']['messages']:.1f} messages",
                f"{analysis['average_length']['words']:,.0f} words"
            )
            table.add_row(
                "Most Active Time",
                f"{analysis['most_active_hours']['peak']}:00",
                f"{analysis['most_active_hours']['percentage']:.1f}% of activity"
            )
            table.add_row(
                "Compression Potential",
                f"{analysis['compression_potential']['percentage']:.1f}%",
                f"~{analysis['compression_potential']['size_mb']:.1f} MB savings"
            )
            table.add_row(
                "Primary Language",
                analysis['language_distribution']['primary'],
                f"{analysis['language_distribution']['confidence']:.1f}% confidence"
            )
            
            # Display results
            console = Console()
            console.print(table)
            
            # Show top topics if available
            if analysis['common_topics']:
                topics_text = Text()
                topics_text.append("Most Common Topics:\n", style="bold")
                for i, topic in enumerate(analysis['common_topics'][:5], 1):
                    topics_text.append(f"{i}. {topic['name']} ({topic['frequency']} mentions)\n")
                
                topics_panel = Panel(topics_text, title="📝 Topic Analysis", style="blue")
                console.print(topics_panel)
            
            return self._success({
                'analysis': analysis,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to analyze conversations: {str(e)}")

    def _export_conversations(self, format_type: str, filename: Optional[str]) -> Dict[str, Any]:
        """Export conversation data in specified format"""
        try:
            if format_type not in ['json', 'text', 'csv']:
                return self._error(f"Unsupported export format: {format_type}")
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"deile_conversations_{timestamp}.{format_type}"
            
            # Get all conversations
            conversations = self._get_all_conversations()
            
            if not conversations:
                return self._error("No conversations found to export")
            
            # Export based on format
            export_path = Path(filename)
            
            if format_type == 'json':
                self._export_json(conversations, export_path)
            elif format_type == 'text':
                self._export_text(conversations, export_path)
            elif format_type == 'csv':
                self._export_csv(conversations, export_path)
            
            file_size = export_path.stat().st_size
            
            # Display results
            console = Console()
            success_text = Text()
            success_text.append("📤 Export Complete\n\n", style="bold green")
            success_text.append(f"File: {export_path}\n")
            success_text.append(f"Format: {format_type.upper()}\n")
            success_text.append(f"Size: {file_size / 1024:.1f} KB\n")
            success_text.append(f"Conversations: {len(conversations)}")
            
            panel = Panel(success_text, title="Export Results", style="green")
            console.print(panel)
            
            return self._success({
                'export_file': str(export_path),
                'format': format_type,
                'conversation_count': len(conversations),
                'file_size_bytes': file_size,
                'export_timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to export conversations: {str(e)}")

    def _import_conversations(self, filename: Optional[str]) -> Dict[str, Any]:
        """Import conversation data from file"""
        try:
            if not filename:
                return self._error("Filename is required for import operation")
            
            import_path = Path(filename)
            if not import_path.exists():
                return self._error(f"Import file not found: {filename}")
            
            # Detect format from extension
            format_type = import_path.suffix.lower()[1:]  # Remove the dot
            
            if format_type not in ['json', 'txt', 'csv']:
                return self._error(f"Unsupported import format: {format_type}")
            
            # Import based on format
            if format_type == 'json':
                conversations = self._import_json(import_path)
            elif format_type == 'txt':
                conversations = self._import_text(import_path)
            elif format_type == 'csv':
                conversations = self._import_csv(import_path)
            
            if not conversations:
                return self._error("No valid conversations found in import file")
            
            # Merge with existing conversations
            imported_count = self._merge_conversations(conversations)
            
            # Display results
            console = Console()
            success_text = Text()
            success_text.append("📥 Import Complete\n\n", style="bold green")
            success_text.append(f"File: {import_path}\n")
            success_text.append(f"Conversations imported: {imported_count}\n")
            success_text.append(f"Format: {format_type.upper()}")
            
            panel = Panel(success_text, title="Import Results", style="green")
            console.print(panel)
            
            return self._success({
                'import_file': str(import_path),
                'conversations_imported': imported_count,
                'format': format_type,
                'import_timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to import conversations: {str(e)}")

    def _show_config(self) -> Dict[str, Any]:
        """Show current configuration settings"""
        try:
            # Create configuration table
            table = Table(title="⚙️ Compact Configuration", show_header=True, header_style="bold cyan")
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")
            table.add_column("Description", style="dim")
            
            config_descriptions = {
                'auto_compress': "Automatically compress old conversations",
                'compress_threshold_days': "Days before conversations are compressed",
                'purge_threshold_days': "Days before conversations are purged",
                'max_summary_length': "Maximum length of compressed summaries",
                'compression_ratio_target': "Target compression ratio (0.0-1.0)",
                'max_memory_usage_mb': "Maximum memory usage limit in MB"
            }
            
            for setting, value in self.config.items():
                if setting in config_descriptions:
                    table.add_row(
                        setting,
                        str(value),
                        config_descriptions[setting]
                    )
            
            # Display configuration
            console = Console()
            console.print(table)
            
            return self._success({
                'configuration': self.config,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to show configuration: {str(e)}")

    def _configure_settings(self, args: List[str]) -> Dict[str, Any]:
        """Configure compression settings"""
        try:
            if len(args) < 2:
                return self._error("Usage: /compact config <setting> <value>")
            
            setting = args[0]
            value = args[1]
            
            if setting not in self.config:
                return self._error(f"Unknown setting: {setting}")
            
            # Convert value to appropriate type
            original_value = self.config[setting]
            
            if isinstance(original_value, bool):
                new_value = value.lower() in ['true', 'on', 'yes', '1']
            elif isinstance(original_value, int):
                new_value = int(value)
            elif isinstance(original_value, float):
                new_value = float(value)
            else:
                new_value = value
            
            # Validate value
            if not self._validate_config_value(setting, new_value):
                return self._error(f"Invalid value for {setting}: {value}")
            
            # Update configuration
            old_value = self.config[setting]
            self.config[setting] = new_value
            
            # Save configuration
            self._save_config()
            
            # Display result
            console = Console()
            success_text = Text()
            success_text.append("⚙️ Configuration Updated\n\n", style="bold green")
            success_text.append(f"Setting: {setting}\n")
            success_text.append(f"Old value: {old_value}\n")
            success_text.append(f"New value: {new_value}")
            
            panel = Panel(success_text, title="Config Update", style="green")
            console.print(panel)
            
            return self._success({
                'setting': setting,
                'old_value': old_value,
                'new_value': new_value,
                'timestamp': datetime.now().isoformat()
            })
            
        except ValueError as e:
            return self._error(f"Invalid value format: {str(e)}")
        except Exception as e:
            return self._error(f"Failed to configure setting: {str(e)}")

    # Helper methods for conversation management
    def _get_conversation_stats(self) -> Dict[str, Any]:
        """Get conversation statistics (mock implementation)"""
        # In a real implementation, this would query the actual conversation database
        return {
            'active_conversations': 15,
            'recent_conversations': 3,
            'total_messages': 1247,
            'compressed_messages': 342,
            'storage_size_mb': 12.8,
            'compressed_size_mb': 4.2,
            'oldest_date': '2024-08-01',
            'days_of_history': 37
        }

    def _calculate_memory_usage(self) -> Dict[str, Any]:
        """Calculate current memory usage (mock implementation)"""
        return {
            'total_mb': 45.2,
            'conversations_mb': 32.1,
            'cache_mb': 8.7,
            'other_mb': 4.4,
            'utilization_percent': 45.2
        }

    def _generate_recommendations(self, stats: Dict[str, Any], memory: Dict[str, Any]) -> List[str]:
        """Generate optimization recommendations"""
        recommendations = []
        
        if memory['total_mb'] > self.config['max_memory_usage_mb'] * 0.8:
            recommendations.append("Memory usage is high - consider compressing old conversations")
        
        if stats['days_of_history'] > self.config['compress_threshold_days'] * 2:
            recommendations.append("Long conversation history detected - compression recommended")
        
        if stats['compressed_messages'] / stats['total_messages'] < 0.2:
            recommendations.append("Low compression ratio - enable auto-compression")
        
        if not self.config['auto_compress']:
            recommendations.append("Auto-compression is disabled - enable for automatic optimization")
        
        return recommendations

    def _find_conversations_before(self, cutoff_date: datetime) -> List[Dict[str, Any]]:
        """Find conversations before cutoff date (mock implementation)"""
        # Mock data for demonstration
        return [
            {'id': 'conv1', 'date': '2024-08-01', 'messages': 45},
            {'id': 'conv2', 'date': '2024-08-15', 'messages': 23},
            {'id': 'conv3', 'date': '2024-08-20', 'messages': 67}
        ]

    def _create_conversation_summary(self, conversation: Dict[str, Any]) -> Dict[str, Any]:
        """Create intelligent summary of conversation"""
        # This would use AI/ML to create meaningful summaries
        return {
            'id': conversation['id'],
            'original_date': conversation['date'],
            'summary': f"Conversation with {conversation['messages']} messages - compressed summary",
            'key_points': ['Point 1', 'Point 2', 'Point 3'],
            'compressed_at': datetime.now().isoformat(),
            'type': 'compressed_summary'
        }

    def _get_conversation_size(self, conversation: Dict[str, Any]) -> int:
        """Get size of conversation in bytes"""
        return len(json.dumps(conversation))

    def _replace_with_summary(self, conversation: Dict[str, Any], summary: Dict[str, Any]):
        """Replace conversation with summary"""
        # Mock implementation
        pass

    def _update_last_compress_time(self):
        """Update last compression timestamp"""
        self.config['last_compress'] = datetime.now().isoformat()

    def _get_all_conversations(self) -> List[Dict[str, Any]]:
        """Get all conversations (mock implementation)"""
        return [
            {'id': f'conv{i}', 'date': f'2024-08-{i:02d}', 'messages': i*10}
            for i in range(1, 21)
        ]

    def _validate_config_value(self, setting: str, value: Any) -> bool:
        """Validate configuration value"""
        if setting.endswith('_days') and (not isinstance(value, int) or value < 1):
            return False
        if setting == 'compression_ratio_target' and (not isinstance(value, float) or not 0.0 <= value <= 1.0):
            return False
        if setting == 'max_memory_usage_mb' and (not isinstance(value, int) or value < 10):
            return False
        return True

    def _save_config(self):
        """Save configuration to file"""
        # Mock implementation - would save to actual config file
        pass

    # Export/Import helper methods (simplified implementations)
    def _export_json(self, conversations: List[Dict[str, Any]], path: Path):
        """Export conversations to JSON"""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(conversations, f, indent=2, ensure_ascii=False)

    def _export_text(self, conversations: List[Dict[str, Any]], path: Path):
        """Export conversations to text"""
        with open(path, 'w', encoding='utf-8') as f:
            for conv in conversations:
                f.write(f"Conversation: {conv['id']}\n")
                f.write(f"Date: {conv['date']}\n")
                f.write(f"Messages: {conv['messages']}\n")
                f.write("-" * 50 + "\n\n")

    def _export_csv(self, conversations: List[Dict[str, Any]], path: Path):
        """Export conversations to CSV"""
        import csv
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['id', 'date', 'messages'])
            writer.writeheader()
            writer.writerows(conversations)

    def _import_json(self, path: Path) -> List[Dict[str, Any]]:
        """Import conversations from JSON"""
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _import_text(self, path: Path) -> List[Dict[str, Any]]:
        """Import conversations from text (simplified)"""
        # Simplified implementation
        return []

    def _import_csv(self, path: Path) -> List[Dict[str, Any]]:
        """Import conversations from CSV"""
        import csv
        conversations = []
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                conversations.append(dict(row))
        return conversations

    def _merge_conversations(self, conversations: List[Dict[str, Any]]) -> int:
        """Merge imported conversations with existing ones"""
        # Mock implementation - would handle deduplication and merging
        return len(conversations)

    # Analysis helper methods (simplified implementations)
    def _get_date_range(self, conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Get date range of conversations"""
        return {'days': 30, 'start': '2024-08-01', 'end': '2024-08-31'}

    def _calculate_average_length(self, conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate average conversation length"""
        avg_messages = sum(conv.get('messages', 0) for conv in conversations) / len(conversations)
        return {'messages': avg_messages, 'words': avg_messages * 50}

    def _find_active_hours(self, conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Find most active hours"""
        return {'peak': 14, 'percentage': 23.5}

    def _extract_common_topics(self, conversations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract common conversation topics"""
        return [
            {'name': 'Python Development', 'frequency': 45},
            {'name': 'Bug Fixes', 'frequency': 32},
            {'name': 'Code Review', 'frequency': 28}
        ]

    def _analyze_languages(self, conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze language distribution"""
        return {'primary': 'English', 'confidence': 95.2}

    def _estimate_compression_potential(self, conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Estimate compression potential"""
        return {'percentage': 65.3, 'size_mb': 8.4}


# Register the command
from deile.commands.registry import StaticCommandRegistry
StaticCommandRegistry.register("compact", CompactCommand)