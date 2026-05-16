"""Sistema de comandos slash do DEILE"""

from .base import CommandContext, CommandResult, CommandStatus, SlashCommand
from .registry import CommandRegistry, get_command_registry

__all__ = [
    "SlashCommand",
    "CommandResult",
    "CommandContext",
    "CommandStatus",
    "CommandRegistry",
    "get_command_registry",
]
