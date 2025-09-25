"""Security module for DEILE"""

from .permissions import PermissionManager, PermissionRule, PermissionLevel, get_permission_manager
from .secrets_scanner import SecretsScanner
from .audit_logger import (
    AuditLogger, AuditEvent, AuditEventType, SeverityLevel,
    get_audit_logger, log_permission_check, log_secret_detection,
    log_tool_execution, log_sandbox_violation, log_plan_execution, log_approval_event
)

__all__ = [
    "PermissionManager",
    "PermissionRule", 
    "PermissionLevel",
    "get_permission_manager",
    "SecretsScanner",
    "AuditLogger",
    "AuditEvent",
    "AuditEventType",
    "SeverityLevel",
    "get_audit_logger",
    "log_permission_check",
    "log_secret_detection",
    "log_tool_execution",
    "log_sandbox_violation",
    "log_plan_execution",
    "log_approval_event"
]