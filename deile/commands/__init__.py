"""Sistema de comandos slash do DEILE"""

from .base import SlashCommand, CommandResult, CommandContext, CommandStatus
from .registry import CommandRegistry, StaticCommandRegistry, get_command_registry
from .actions import CommandActions

__all__ = [
    "SlashCommand",
    "CommandResult", 
    "CommandContext",
    "CommandStatus",
    "CommandRegistry",
    "StaticCommandRegistry",
    "get_command_registry",
    "CommandActions"
]