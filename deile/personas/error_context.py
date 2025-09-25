"""Error context system for persona operations with rich context and recovery support"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional
from enum import Enum
import uuid
import traceback


class ErrorSeverity(Enum):
    """Error severity levels for persona operations"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ErrorContext:
    """Rich error context for persona operations with recovery information"""

    # Unique identifiers
    error_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    correlation_id: Optional[str] = None

    # Error classification
    severity: ErrorSeverity = ErrorSeverity.MEDIUM
    error_type: str = "unknown"
    error_code: Optional[str] = None

    # Operation context
    component: str = "persona_system"
    operation: str = ""
    persona_id: Optional[str] = None

    # User and session context
    user_id: Optional[str] = None
    session_id: Optional[str] = None

    # System state context
    system_state: Dict[str, Any] = field(default_factory=dict)
    operation_parameters: Dict[str, Any] = field(default_factory=dict)

    # Recovery information
    recovery_suggestions: List[str] = field(default_factory=list)
    auto_recovery_attempted: bool = False
    recovery_success: Optional[bool] = None
    recovery_strategy_used: Optional[str] = None

    # Audit and tracing
    audit_correlation_id: Optional[str] = None
    stack_trace: Optional[str] = None

    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_recovery_suggestion(self, suggestion: str) -> None:
        """Add a recovery suggestion"""
        if suggestion not in self.recovery_suggestions:
            self.recovery_suggestions.append(suggestion)

    def mark_auto_recovery_attempted(self, success: bool, strategy: str = None) -> None:
        """Mark that automatic recovery was attempted"""
        self.auto_recovery_attempted = True
        self.recovery_success = success
        if strategy:
            self.recovery_strategy_used = strategy

    def add_system_context(self, key: str, value: Any) -> None:
        """Add system state context"""
        self.system_state[key] = value

    def add_metadata(self, key: str, value: Any) -> None:
        """Add additional metadata"""
        # Handle potential circular references by converting to string if needed
        try:
            # Try to serialize to check for circular references
            import json
            json.dumps(value, default=str)
            self.metadata[key] = value
        except (TypeError, ValueError):
            # If serialization fails, convert to string representation
            self.metadata[key] = str(value)

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set correlation ID for linking related errors"""
        self.correlation_id = correlation_id

    def set_audit_correlation_id(self, audit_id: str) -> None:
        """Set audit correlation ID for audit trail"""
        self.audit_correlation_id = audit_id

    def capture_stack_trace(self) -> None:
        """Capture current stack trace for debugging"""
        self.stack_trace = traceback.format_exc() if traceback.format_exc().strip() != "NoneType: None" else None

    def to_audit_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for audit logging"""
        return {
            'error_id': self.error_id,
            'timestamp': self.timestamp.isoformat(),
            'correlation_id': self.correlation_id,
            'severity': self.severity.value,
            'error_type': self.error_type,
            'error_code': self.error_code,
            'component': self.component,
            'operation': self.operation,
            'persona_id': self.persona_id,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'recovery_attempted': self.auto_recovery_attempted,
            'recovery_success': self.recovery_success,
            'recovery_strategy': self.recovery_strategy_used,
            'audit_correlation_id': self.audit_correlation_id,
            'metadata': self.metadata
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'error_id': self.error_id,
            'timestamp': self.timestamp.isoformat(),
            'correlation_id': self.correlation_id,
            'severity': self.severity.value,
            'error_type': self.error_type,
            'error_code': self.error_code,
            'component': self.component,
            'operation': self.operation,
            'persona_id': self.persona_id,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'system_state': self.system_state,
            'operation_parameters': self.operation_parameters,
            'recovery_suggestions': self.recovery_suggestions,
            'auto_recovery_attempted': self.auto_recovery_attempted,
            'recovery_success': self.recovery_success,
            'recovery_strategy_used': self.recovery_strategy_used,
            'audit_correlation_id': self.audit_correlation_id,
            'metadata': self.metadata,
            'stack_trace': self.stack_trace
        }

    @classmethod
    def from_persona_error(cls, error, **kwargs):
        """Create ErrorContext from PersonaError"""
        from ..core.exceptions import PersonaError

        if not isinstance(error, PersonaError):
            raise TypeError("Error must be a PersonaError instance")

        context = cls(
            error_type=error.__class__.__name__,
            error_code=error.error_code,
            operation=error.operation or "unknown",
            persona_id=error.persona_id,
            **kwargs
        )

        # Set severity based on error type
        context.severity = cls._determine_severity_from_error(error)

        # Add recovery suggestion if available
        if hasattr(error, 'recovery_suggestion') and error.recovery_suggestion:
            context.add_recovery_suggestion(error.recovery_suggestion)

        # Add error context
        if hasattr(error, 'context'):
            for key, value in error.context.items():
                context.add_metadata(key, value)

        return context

    @staticmethod
    def _determine_severity_from_error(error) -> ErrorSeverity:
        """Determine severity level from error type"""
        from ..core.exceptions import (
            PersonaConfigError, PersonaLoadError, PersonaSwitchError,
            PersonaExecutionError, PersonaInitializationError, PersonaIntegrationError
        )

        severity_mapping = {
            PersonaConfigError: ErrorSeverity.HIGH,
            PersonaInitializationError: ErrorSeverity.HIGH,
            PersonaIntegrationError: ErrorSeverity.HIGH,
            PersonaLoadError: ErrorSeverity.MEDIUM,
            PersonaSwitchError: ErrorSeverity.MEDIUM,
            PersonaExecutionError: ErrorSeverity.LOW,
        }

        return severity_mapping.get(type(error), ErrorSeverity.MEDIUM)