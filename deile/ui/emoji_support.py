"""Emoji support for cross-platform compatibility

This module provides intelligent emoji rendering that falls back to text
equivalents on systems that don't support Unicode emojis properly.
"""

import sys
import os
from typing import Dict, Optional


class EmojiManager:
    """Manages emoji display with fallbacks for Windows terminal"""
    
    def __init__(self):
        self.supports_emoji = self._check_emoji_support()
        self.emoji_map = self._build_emoji_map()
    
    def _check_emoji_support(self) -> bool:
        """Check if the current terminal supports emojis"""
        # Check environment variables that indicate emoji support
        if os.getenv('TERM_PROGRAM') in ['iTerm.app', 'Apple_Terminal']:
            return True
        
        if os.getenv('COLORTERM') in ['truecolor', '24bit']:
            return True
        
        # Windows Terminal and modern terminals
        if os.getenv('WT_SESSION'):  # Windows Terminal
            return True
            
        if os.getenv('TERM_PROGRAM') == 'vscode':  # VS Code terminal
            return True
        
        # Try to enable Unicode support on Windows
        if sys.platform == "win32":
            try:
                # Try to set console to UTF-8
                os.system('chcp 65001 >nul 2>&1')
                # Most modern Windows terminals support emojis now
                return True
            except:
                return False
        
        # Linux/Mac terminals generally support emojis
        if sys.platform in ["linux", "darwin"]:
            return True
        
        return False
    
    def _build_emoji_map(self) -> Dict[str, str]:
        """Build mapping of emoji names to characters or fallback text"""
        emoji_fallbacks = {
            # Status emojis
            'success': '✅' if self.supports_emoji else '[OK]',
            'error': '❌' if self.supports_emoji else '[ERROR]',
            'warning': '⚠️' if self.supports_emoji else '[WARNING]',
            'info': 'ℹ️' if self.supports_emoji else '[INFO]',
            'loading': '⏳' if self.supports_emoji else '[...]',
            'processing': '⚡' if self.supports_emoji else '[PROC]',
            
            # Actions
            'create': '📝' if self.supports_emoji else '[CREATE]',
            'read': '📖' if self.supports_emoji else '[READ]',
            'write': '✏️' if self.supports_emoji else '[WRITE]',
            'delete': '🗑️' if self.supports_emoji else '[DELETE]',
            'search': '🔍' if self.supports_emoji else '[SEARCH]',
            'execute': '⚙️' if self.supports_emoji else '[EXEC]',
            
            # Files
            'python': '🐍' if self.supports_emoji else '[PY]',
            'javascript': '💛' if self.supports_emoji else '[JS]',
            'html': '🌐' if self.supports_emoji else '[HTML]',
            'css': '🎨' if self.supports_emoji else '[CSS]',
            'markdown': '📝' if self.supports_emoji else '[MD]',
            'text': '📄' if self.supports_emoji else '[TXT]',
            'json': '⚙️' if self.supports_emoji else '[JSON]',
            'image': '🖼️' if self.supports_emoji else '[IMG]',
            'file': '📄' if self.supports_emoji else '[FILE]',
            
            # Interface
            'sparkles': '✨' if self.supports_emoji else '*',
            'rocket': '🚀' if self.supports_emoji else '^',
            'wave': '👋' if self.supports_emoji else 'Bye',
            'robot': '🤖' if self.supports_emoji else '[AI]',
            'brain': '🧠' if self.supports_emoji else '[BRAIN]',
            'magic': '✨' if self.supports_emoji else '[*]',
            'gear': '⚙️' if self.supports_emoji else '[CFG]',
            'chart': '📊' if self.supports_emoji else '[STATS]',
            'clock': '⏱️' if self.supports_emoji else '[TIME]',
            'tool': '🔧' if self.supports_emoji else '[TOOL]',
            'lightbulb': '💡' if self.supports_emoji else '[IDEA]',
            
            # Special
            'deile_logo': '🔮' if self.supports_emoji else '[DEILE]',
            'ai_agent': '🤖' if self.supports_emoji else '[AGENT]'
        }
        
        return emoji_fallbacks
    
    def get(self, emoji_name: str, fallback: Optional[str] = None) -> str:
        """Get emoji or fallback text"""
        if emoji_name in self.emoji_map:
            return self.emoji_map[emoji_name]
        
        if fallback:
            return fallback if not self.supports_emoji else emoji_name
        
        return emoji_name
    
    def format_text(self, text: str) -> str:
        """Replace emoji placeholders in text with appropriate characters"""
        # Simple replacement for common patterns
        replacements = {
            ':success:': self.get('success'),
            ':error:': self.get('error'),
            ':warning:': self.get('warning'),
            ':info:': self.get('info'),
            ':sparkles:': self.get('sparkles'),
            ':wave:': self.get('wave'),
            ':robot:': self.get('robot'),
            ':rocket:': self.get('rocket'),
            ':tool:': self.get('tool'),
            ':clock:': self.get('clock'),
            ':lightbulb:': self.get('lightbulb')
        }
        
        formatted_text = text
        for placeholder, emoji in replacements.items():
            formatted_text = formatted_text.replace(placeholder, emoji)
        
        return formatted_text
    
    def enable_unicode_console(self) -> bool:
        """Try to enable Unicode support on Windows console"""
        if sys.platform == "win32":
            try:
                # Set console code page to UTF-8
                os.system('chcp 65001 >nul 2>&1')
                
                # Set environment variables for better Unicode support
                os.environ['PYTHONIOENCODING'] = 'utf-8'
                
                return True
            except:
                return False
        return True


# Global emoji manager instance
_emoji_manager: Optional[EmojiManager] = None


def get_emoji_manager() -> EmojiManager:
    """Get global emoji manager instance"""
    global _emoji_manager
    if _emoji_manager is None:
        _emoji_manager = EmojiManager()
    return _emoji_manager


def emoji(name: str, fallback: Optional[str] = None) -> str:
    """Quick helper to get emoji"""
    return get_emoji_manager().get(name, fallback)


def format_with_emojis(text: str) -> str:
    """Format text with emoji replacements"""
    return get_emoji_manager().format_text(text)


# Common emoji shortcuts
SUCCESS = lambda: emoji('success')
ERROR = lambda: emoji('error') 
WARNING = lambda: emoji('warning')
INFO = lambda: emoji('info')
SPARKLES = lambda: emoji('sparkles')
WAVE = lambda: emoji('wave')
ROBOT = lambda: emoji('robot')
ROCKET = lambda: emoji('rocket')
TOOL = lambda: emoji('tool')
CLOCK = lambda: emoji('clock')
LIGHTBULB = lambda: emoji('lightbulb')