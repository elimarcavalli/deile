"""Audit integration for persona errors with comprehensive security event logging"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from .error_context import ErrorContext, ErrorSeverity
from ..core.exceptions import PersonaError
from ..security.audit_logger import get_audit_logger, AuditEventType, SeverityLevel

logger = logging.getLogger(__name__)


class PersonaErrorAuditLogger:
    """Specialized audit logging for persona errors with security event integration"""

    def __init__(self):
        self.audit_logger = get_audit_logger()

    async def log_persona_error(
        self,
        error: PersonaError,
        context: ErrorContext
    ) -> None:
        """Log persona error to DEILE audit system with rich context"""
        try:
            # Map persona error severity to audit severity
            audit_severity = self._map_severity(context.severity)

            # Determine specific audit event type
            event_type = self._determine_event_type(error)

            # Prepare comprehensive audit event data
            audit_data = {
                'error_type': error.__class__.__name__,
                'error_code': error.error_code,
                'error_message': str(error),
                'persona_id': error.persona_id,
                'operation': error.operation,
                'error_id': context.error_id,
                'timestamp': context.timestamp.isoformat(),
                'user_id': context.user_id,
                'session_id': context.session_id,
                'correlation_id': context.correlation_id,
                'recovery_attempted': context.auto_recovery_attempted,
                'recovery_success': context.recovery_success,
                'recovery_strategy': context.recovery_strategy_used,
                'system_state': context.system_state,
                'operation_parameters': context.operation_parameters,
                'recovery_suggestions': context.recovery_suggestions,
                'metadata': context.metadata
            }

            # Log to DEILE audit system
            self.audit_logger.log_event(
                event_type=event_type,
                severity=audit_severity,
                actor="persona_system",
                resource=f"persona:{error.persona_id}" if error.persona_id else "persona_system",
                action=error.operation or "unknown",
                result="error",
                details=audit_data,
                run_id=context.session_id,
                plan_id=None,
                tool_name="persona_manager"
            )

            # Set audit correlation ID in context
            if context.audit_correlation_id is None:
                context.set_audit_correlation_id(context.error_id)

            logger.info(f"Persona error logged to audit system: {context.error_id}")

        except Exception as audit_error:
            logger.error(f"Failed to log persona error to audit system: {audit_error}")
            # Don't re-raise to avoid error logging loops
            # Try to log the audit failure itself to a fallback logger
            try:
                logger.critical(f"AUDIT SYSTEM FAILURE - Error: {error.error_code}, Context: {context.error_id}, Audit Error: {audit_error}")
            except Exception:
                # If even critical logging fails, print to stderr as last resort
                import sys
                print(f"CRITICAL: Audit system completely failed - {audit_error}", file=sys.stderr)

    async def log_persona_load(
        self,
        persona_id: str,
        success: bool,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        load_time_ms: Optional[int] = None,
        error_details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log persona load operation"""
        try:
            audit_data = {
                'persona_id': persona_id,
                'success': success,
                'load_time_ms': load_time_ms,
                'error_details': error_details or {}
            }

            self.audit_logger.log_event(
                event_type=AuditEventType.PERSONA_LOAD,
                severity=SeverityLevel.INFO if success else SeverityLevel.WARNING,
                actor="persona_manager",
                resource=f"persona:{persona_id}",
                action="load",
                result="success" if success else "failure",
                details=audit_data,
                run_id=session_id,
                tool_name="persona_manager"
            )

        except Exception as audit_error:
            logger.error(f"Failed to log persona load event: {audit_error}")

    async def log_persona_switch(
        self,
        from_persona: str,
        to_persona: str,
        success: bool,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        switch_reason: Optional[str] = None
    ) -> None:
        """Log persona switch operation"""
        try:
            audit_data = {
                'from_persona': from_persona,
                'to_persona': to_persona,
                'success': success,
                'switch_reason': switch_reason
            }

            self.audit_logger.log_event(
                event_type=AuditEventType.PERSONA_SWITCH,
                severity=SeverityLevel.INFO if success else SeverityLevel.WARNING,
                actor=user_id or "system",
                resource=f"persona:{to_persona}",
                action="switch",
                result="success" if success else "failure",
                details=audit_data,
                run_id=session_id,
                tool_name="persona_manager"
            )

        except Exception as audit_error:
            logger.error(f"Failed to log persona switch event: {audit_error}")

    async def log_recovery_attempt(
        self,
        error: PersonaError,
        context: ErrorContext,
        recovery_action: str,
        success: bool
    ) -> None:
        """Log persona error recovery attempt"""
        try:
            audit_data = {
                'error_id': context.error_id,
                'error_code': error.error_code,
                'recovery_action': recovery_action,
                'success': success,
                'persona_id': error.persona_id,
                'operation': error.operation,
                'attempt_timestamp': datetime.now().isoformat(),
                'recovery_metadata': context.metadata
            }

            event_type = AuditEventType.PERSONA_RECOVERY_SUCCESS if success else AuditEventType.PERSONA_RECOVERY_FAILURE
            severity = SeverityLevel.INFO if success else SeverityLevel.WARNING

            self.audit_logger.log_event(
                event_type=event_type,
                severity=severity,
                actor="error_recovery_manager",
                resource=f"persona:{error.persona_id}" if error.persona_id else "persona_system",
                action="error_recovery",
                result="success" if success else "failure",
                details=audit_data,
                run_id=context.session_id,
                tool_name="error_recovery_manager"
            )

        except Exception as audit_error:
            logger.error(f"Failed to log recovery attempt: {audit_error}")

    async def log_persona_config_error(
        self,
        persona_id: str,
        config_error: str,
        config_key: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> None:
        """Log persona configuration error"""
        try:
            audit_data = {
                'persona_id': persona_id,
                'config_error': config_error,
                'config_key': config_key
            }

            self.audit_logger.log_event(
                event_type=AuditEventType.PERSONA_CONFIG_ERROR,
                severity=SeverityLevel.ERROR,
                actor="persona_config_manager",
                resource=f"persona_config:{persona_id}",
                action="validate_config",
                result="error",
                details=audit_data,
                run_id=session_id,
                tool_name="persona_config_manager"
            )

        except Exception as audit_error:
            logger.error(f"Failed to log persona config error: {audit_error}")

    async def log_persona_execution_error(
        self,
        persona_id: str,
        capability: str,
        execution_error: str,
        session_id: Optional[str] = None
    ) -> None:
        """Log persona capability execution error"""
        try:
            audit_data = {
                'persona_id': persona_id,
                'capability': capability,
                'execution_error': execution_error
            }

            self.audit_logger.log_event(
                event_type=AuditEventType.PERSONA_EXECUTION_ERROR,
                severity=SeverityLevel.WARNING,
                actor=f"persona:{persona_id}",
                resource=f"capability:{capability}",
                action="execute_capability",
                result="error",
                details=audit_data,
                run_id=session_id,
                tool_name="persona_capability_executor"
            )

        except Exception as audit_error:
            logger.error(f"Failed to log persona execution error: {audit_error}")

    def _map_severity(self, error_severity: ErrorSeverity) -> SeverityLevel:
        """Map persona error severity to audit system severity"""
        mapping = {
            ErrorSeverity.LOW: SeverityLevel.INFO,
            ErrorSeverity.MEDIUM: SeverityLevel.WARNING,
            ErrorSeverity.HIGH: SeverityLevel.ERROR,
            ErrorSeverity.CRITICAL: SeverityLevel.CRITICAL
        }
        return mapping.get(error_severity, SeverityLevel.WARNING)

    def _determine_event_type(self, error: PersonaError) -> AuditEventType:
        """Determine specific audit event type based on error type"""
        from ..core.exceptions import (
            PersonaLoadError, PersonaSwitchError, PersonaConfigError,
            PersonaExecutionError, PersonaInitializationError, PersonaIntegrationError
        )

        event_type_mapping = {
            PersonaLoadError: AuditEventType.PERSONA_LOAD,
            PersonaSwitchError: AuditEventType.PERSONA_SWITCH,
            PersonaConfigError: AuditEventType.PERSONA_CONFIG_ERROR,
            PersonaExecutionError: AuditEventType.PERSONA_EXECUTION_ERROR,
            PersonaInitializationError: AuditEventType.PERSONA_ERROR,
            PersonaIntegrationError: AuditEventType.PERSONA_ERROR,
        }

        return event_type_mapping.get(type(error), AuditEventType.PERSONA_ERROR)

    async def get_persona_audit_summary(
        self,
        persona_id: Optional[str] = None,
        time_range_hours: int = 24
    ) -> Dict[str, Any]:
        """Get audit summary for persona operations"""
        try:
            # Get recent persona-related events from audit logger
            recent_events = self.audit_logger.get_recent_events(
                limit=1000,
                actor="persona_system" if not persona_id else f"persona:{persona_id}"
            )

            # Filter events by time range
            cutoff_time = datetime.now().timestamp() - (time_range_hours * 3600)
            relevant_events = [
                event for event in recent_events
                if event.timestamp.timestamp() > cutoff_time
            ]

            # Count events by type
            event_counts = {}
            error_counts = {}
            recovery_attempts = 0
            recovery_successes = 0

            for event in relevant_events:
                event_type = event.event_type.value
                event_counts[event_type] = event_counts.get(event_type, 0) + 1

                if event.event_type in [
                    AuditEventType.PERSONA_ERROR,
                    AuditEventType.PERSONA_CONFIG_ERROR,
                    AuditEventType.PERSONA_EXECUTION_ERROR
                ]:
                    error_counts[event_type] = error_counts.get(event_type, 0) + 1

                elif event.event_type == AuditEventType.PERSONA_RECOVERY_SUCCESS:
                    recovery_attempts += 1
                    recovery_successes += 1
                elif event.event_type == AuditEventType.PERSONA_RECOVERY_FAILURE:
                    recovery_attempts += 1

            # Calculate recovery success rate
            recovery_success_rate = (
                recovery_successes / recovery_attempts * 100
                if recovery_attempts > 0 else 0
            )

            return {
                'persona_id': persona_id,
                'time_range_hours': time_range_hours,
                'total_events': len(relevant_events),
                'event_counts': event_counts,
                'error_counts': error_counts,
                'recovery_attempts': recovery_attempts,
                'recovery_successes': recovery_successes,
                'recovery_success_rate': f"{recovery_success_rate:.1f}%",
                'summary_generated_at': datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to generate persona audit summary: {e}")
            return {
                'error': f"Failed to generate audit summary: {str(e)}",
                'summary_generated_at': datetime.now().isoformat()
            }


# Global persona audit logger instance
_persona_audit_logger = None


def get_persona_audit_logger() -> PersonaErrorAuditLogger:
    """Get global persona audit logger instance"""
    global _persona_audit_logger
    if _persona_audit_logger is None:
        _persona_audit_logger = PersonaErrorAuditLogger()
    return _persona_audit_logger


# Convenience functions for common audit operations
async def log_persona_error(error: PersonaError, context: ErrorContext) -> None:
    """Convenience function for logging persona errors"""
    await get_persona_audit_logger().log_persona_error(error, context)


async def log_persona_load(persona_id: str, success: bool, **kwargs) -> None:
    """Convenience function for logging persona load operations"""
    await get_persona_audit_logger().log_persona_load(persona_id, success, **kwargs)


async def log_persona_switch(from_persona: str, to_persona: str, success: bool, **kwargs) -> None:
    """Convenience function for logging persona switches"""
    await get_persona_audit_logger().log_persona_switch(from_persona, to_persona, success, **kwargs)


async def log_recovery_attempt(error: PersonaError, context: ErrorContext, recovery_action: str, success: bool) -> None:
    """Convenience function for logging recovery attempts"""
    await get_persona_audit_logger().log_recovery_attempt(error, context, recovery_action, success)