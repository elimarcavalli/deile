"""Comandos builtin do DEILE"""

# Import dos comandos builtin para registro automático
from .help_command import HelpCommand
from .debug_command import DebugCommand  
from .clear_command import ClearCommand
from .status_command import StatusCommand
from .config_command import ConfigCommand

__all__ = [
    "HelpCommand",
    "DebugCommand", 
    "ClearCommand",
    "StatusCommand",
    "ConfigCommand"
]