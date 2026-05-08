"""Comandos builtin do DEILE"""

# Import dos comandos builtin para registro automático
from .apply_command import ApplyCommand
from .approve_command import ApproveCommand
from .clear_command import ClearCommand
from .config_command import ConfigCommand
from .context_command import ContextCommand
from .cost_command import CostCommand
from .debug_command import DebugCommand
from .diff_command import DiffCommand
from .export_command import ExportCommand
from .help_command import HelpCommand
from .model_command import ModelCommand
from .patch_command import PatchCommand
from .permissions_command import PermissionsCommand
from .plan_command import PlanCommand
from .run_command import RunCommand
from .status_command import StatusCommand
from .stop_command import StopCommand
from .tools_command import ToolsCommand

try:
    from .sandbox_command import SandboxCommand
except ImportError:
    SandboxCommand = None  # type: ignore[assignment,misc]
from .logs_command import LogsCommand
from .memory_command import MemoryCommand
from .pipeline_command import PipelineCommand
from .pipeline_schedule_command import PipelineScheduleCommand
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
    "WelcomeCommand",
    "PipelineCommand",
    "PipelineScheduleCommand",
]