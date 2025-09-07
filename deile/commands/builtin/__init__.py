"""Comandos builtin do DEILE"""

# Import dos comandos builtin para registro automático
from .help_command import HelpCommand
from .debug_command import DebugCommand  
from .clear_command import ClearCommand
from .status_command import StatusCommand
from .config_command import ConfigCommand
from .context_command import ContextCommand
from .cost_command import CostCommand
from .tools_command import ToolsCommand
from .model_command import ModelCommand
from .export_command import ExportCommand
from .plan_command import PlanCommand
from .run_command import RunCommand
from .approve_command import ApproveCommand
from .stop_command import StopCommand
from .diff_command import DiffCommand
from .patch_command import PatchCommand
from .apply_command import ApplyCommand
from .permissions_command import PermissionsCommand
from .sandbox_command import SandboxCommand
from .logs_command import LogsCommand
from .memory_command import MemoryCommand
from .welcome_command import WelcomeCommand

__all__ = [
    "HelpCommand",
    "DebugCommand", 
    "ClearCommand",
    "StatusCommand",
    "ConfigCommand",
    "ContextCommand",
    "CostCommand",
    "ToolsCommand",
    "ModelCommand",
    "ExportCommand",
    "PlanCommand",
    "RunCommand",
    "ApproveCommand",
    "StopCommand", 
    "DiffCommand",
    "PatchCommand",
    "ApplyCommand",
    "PermissionsCommand",
    "SandboxCommand",
    "LogsCommand",
    "MemoryCommand",
    "WelcomeCommand"
]