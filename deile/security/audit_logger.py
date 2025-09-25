"""Audit Logger for DEILE Security Events"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass, asdict
from enum import Enum


class AuditEventType(Enum):
    """Types of auditable security events"""
    PERMISSION_CHECK = "permission_check"
    PERMISSION_DENIED = "permission_denied"
    SECRET_DETECTED = "secret_detected"
    SECRET_REDACTED = "secret_redacted"
    SANDBOX_VIOLATION = "sandbox_violation"
    TOOL_EXECUTION = "tool_execution"
    PLAN_EXECUTION = "plan_execution"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    SECURITY_POLICY_CHANGED = "security_policy_changed"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"

    # Persona-specific audit events
    PERSONA_ERROR = "persona_error"
    PERSONA_LOAD = "persona_load"
    PERSONA_SWITCH = "persona_switch"
    PERSONA_CONFIG_ERROR = "persona_config_error"
    PERSONA_EXECUTION_ERROR = "persona_execution_error"
    PERSONA_RECOVERY_SUCCESS = "persona_recovery_success"
    PERSONA_RECOVERY_FAILURE = "persona_recovery_failure"


class SeverityLevel(Enum):
    """Security event severity levels"""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class AuditEvent:
    """Single audit event"""
    timestamp: datetime
    event_type: AuditEventType
    severity: SeverityLevel
    actor: str  # tool name, user, system
    resource: str  # file, command, network endpoint
    action: str  # read, write, execute, etc.
    result: str  # allowed, denied, completed, failed
    details: Dict[str, Any]
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    plan_id: Optional[str] = None
    tool_name: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        data['event_type'] = self.event_type.value
        data['severity'] = self.severity.value
        return data


class AuditLogger:
    """Security audit logger with structured logging and redaction tracking"""
    
    def __init__(self, log_dir: str = "logs", log_file: str = "security_audit.log"):
        self.log_dir = Path(log_dir)
        self.log_file = self.log_dir / log_file
        self.session_id = self._generate_session_id()
        
        # Ensure log directory exists
        self.log_dir.mkdir(exist_ok=True)
        
        # Setup structured logging
        self.logger = logging.getLogger("deile.security.audit")
        self.logger.setLevel(logging.INFO)
        
        # Remove existing handlers to avoid duplicates
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # File handler for structured logs
        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        
        # JSON formatter for structured data
        formatter = logging.Formatter('%(message)s')
        file_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.propagate = False
        
        # Track events in memory for quick access
        self.recent_events: List[AuditEvent] = []
        self.max_memory_events = 1000
        
        # Initialize with session start event
        self.log_event(
            event_type=AuditEventType.TOOL_EXECUTION,
            severity=SeverityLevel.INFO,
            actor="system",
            resource="audit_logger", 
            action="initialize",
            result="success",
            details={"session_id": self.session_id, "log_file": str(self.log_file)}
        )
    
    def _generate_session_id(self) -> str:
        """Generate unique session identifier"""
        from datetime import datetime
        return f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    def log_event(self, 
                  event_type: AuditEventType,
                  severity: SeverityLevel,
                  actor: str,
                  resource: str,
                  action: str,
                  result: str,
                  details: Optional[Dict[str, Any]] = None,
                  run_id: Optional[str] = None,
                  plan_id: Optional[str] = None,
                  tool_name: Optional[str] = None) -> None:
        """Log a security audit event"""
        
        event = AuditEvent(
            timestamp=datetime.now(),
            event_type=event_type,
            severity=severity,
            actor=actor,
            resource=resource,
            action=action,
            result=result,
            details=details or {},
            session_id=self.session_id,
            run_id=run_id,
            plan_id=plan_id,
            tool_name=tool_name
        )
        
        # Add to memory buffer
        self.recent_events.append(event)
        if len(self.recent_events) > self.max_memory_events:
            self.recent_events.pop(0)  # Remove oldest
        
        # Write to structured log file
        log_entry = json.dumps(event.to_dict(), ensure_ascii=False)
        self.logger.info(log_entry)
    
    def log_permission_check(self, 
                           tool_name: str, 
                           resource: str, 
                           action: str, 
                           allowed: bool,
                           rule_id: Optional[str] = None,
                           additional_details: Optional[Dict[str, Any]] = None) -> None:
        """Log permission check result"""
        
        details = {
            "rule_id": rule_id,
            "permission_level": "granted" if allowed else "denied"
        }
        if additional_details:
            details.update(additional_details)
        
        self.log_event(
            event_type=AuditEventType.PERMISSION_DENIED if not allowed else AuditEventType.PERMISSION_CHECK,
            severity=SeverityLevel.WARNING if not allowed else SeverityLevel.INFO,
            actor=tool_name,
            resource=resource,
            action=action,
            result="allowed" if allowed else "denied",
            details=details,
            tool_name=tool_name
        )
    
    def log_secret_detection(self, 
                           file_path: str,
                           secret_type: str,
                           line_number: int,
                           confidence: float,
                           redacted: bool = True) -> None:
        """Log secret detection and redaction"""
        
        details = {
            "secret_type": secret_type,
            "line_number": line_number,
            "confidence": confidence,
            "redacted": redacted,
            "detection_method": "pattern_matching"
        }
        
        event_type = AuditEventType.SECRET_REDACTED if redacted else AuditEventType.SECRET_DETECTED
        severity = SeverityLevel.WARNING if redacted else SeverityLevel.ERROR
        
        self.log_event(
            event_type=event_type,
            severity=severity,
            actor="secrets_scanner",
            resource=file_path,
            action="scan",
            result="redacted" if redacted else "detected",
            details=details
        )
    
    def log_sandbox_violation(self,
                            tool_name: str,
                            violated_resource: str,
                            violation_type: str,
                            blocked: bool = True) -> None:
        """Log sandbox policy violation"""
        
        details = {
            "violation_type": violation_type,
            "enforcement_action": "blocked" if blocked else "allowed_with_warning"
        }
        
        self.log_event(
            event_type=AuditEventType.SANDBOX_VIOLATION,
            severity=SeverityLevel.WARNING if blocked else SeverityLevel.ERROR,
            actor=tool_name,
            resource=violated_resource,
            action="access_attempt",
            result="blocked" if blocked else "allowed",
            details=details,
            tool_name=tool_name
        )
    
    def log_tool_execution(self,
                         tool_name: str,
                         resource: str,
                         success: bool,
                         duration_ms: Optional[int] = None,
                         exit_code: Optional[int] = None,
                         output_size: Optional[int] = None) -> None:
        """Log tool execution event"""
        
        details = {
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "output_size_bytes": output_size
        }
        
        self.log_event(
            event_type=AuditEventType.TOOL_EXECUTION,
            severity=SeverityLevel.INFO if success else SeverityLevel.ERROR,
            actor=tool_name,
            resource=resource,
            action="execute",
            result="success" if success else "failure",
            details=details,
            tool_name=tool_name
        )
    
    def log_plan_execution(self,
                         plan_id: str,
                         action: str,  # start, complete, pause, stop
                         result: str,
                         step_count: Optional[int] = None,
                         duration_ms: Optional[int] = None) -> None:
        """Log plan execution events"""
        
        details = {
            "step_count": step_count,
            "duration_ms": duration_ms
        }
        
        self.log_event(
            event_type=AuditEventType.PLAN_EXECUTION,
            severity=SeverityLevel.INFO,
            actor="plan_manager",
            resource=f"plan:{plan_id}",
            action=action,
            result=result,
            details=details,
            plan_id=plan_id
        )
    
    def log_approval_event(self,
                         plan_id: str,
                         step_id: str,
                         approval_action: str,  # required, granted, denied
                         tool_name: str,
                         risk_level: str,
                         approver: str = "user") -> None:
        """Log approval-related events"""
        
        details = {
            "step_id": step_id,
            "risk_level": risk_level,
            "approver": approver
        }
        
        event_mapping = {
            "required": AuditEventType.APPROVAL_REQUIRED,
            "granted": AuditEventType.APPROVAL_GRANTED,
            "denied": AuditEventType.APPROVAL_DENIED
        }
        
        self.log_event(
            event_type=event_mapping.get(approval_action, AuditEventType.APPROVAL_REQUIRED),
            severity=SeverityLevel.INFO,
            actor=approver,
            resource=f"plan:{plan_id}:step:{step_id}",
            action=approval_action,
            result="logged",
            details=details,
            plan_id=plan_id,
            tool_name=tool_name
        )
    
    def get_recent_events(self, 
                        limit: int = 100,
                        event_type: Optional[AuditEventType] = None,
                        severity: Optional[SeverityLevel] = None,
                        actor: Optional[str] = None) -> List[AuditEvent]:
        """Get recent audit events with optional filtering"""
        
        events = self.recent_events
        
        # Apply filters
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if severity:
            events = [e for e in events if e.severity == severity]
        if actor:
            events = [e for e in events if actor.lower() in e.actor.lower()]
        
        # Return most recent first, limited
        return list(reversed(events))[:limit]
    
    def get_security_summary(self) -> Dict[str, Any]:
        """Get security summary statistics"""
        
        total_events = len(self.recent_events)
        if total_events == 0:
            return {"total_events": 0, "summary": "No events recorded"}
        
        # Count by type
        type_counts = {}
        for event in self.recent_events:
            event_type = event.event_type.value
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
        
        # Count by severity
        severity_counts = {}
        for event in self.recent_events:
            severity = event.severity.value
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        
        # Recent critical events
        critical_events = [
            e for e in self.recent_events 
            if e.severity in [SeverityLevel.ERROR, SeverityLevel.CRITICAL]
        ][-10:]  # Last 10 critical events
        
        # Permission denials
        permission_denials = len([
            e for e in self.recent_events 
            if e.event_type == AuditEventType.PERMISSION_DENIED
        ])
        
        # Secret detections
        secret_detections = len([
            e for e in self.recent_events 
            if e.event_type in [AuditEventType.SECRET_DETECTED, AuditEventType.SECRET_REDACTED]
        ])
        
        return {
            "total_events": total_events,
            "session_id": self.session_id,
            "event_types": type_counts,
            "severity_levels": severity_counts,
            "permission_denials": permission_denials,
            "secret_detections": secret_detections,
            "recent_critical_events": len(critical_events),
            "log_file": str(self.log_file)
        }
    
    def export_audit_log(self, 
                        output_file: str, 
                        format: str = "json",
                        include_details: bool = True) -> str:
        """Export audit log to file"""
        
        output_path = Path(output_file)
        
        if format.lower() == "json":
            # Export as JSON lines
            with open(output_path, 'w', encoding='utf-8') as f:
                for event in self.recent_events:
                    event_data = event.to_dict()
                    if not include_details:
                        event_data.pop('details', None)
                    f.write(json.dumps(event_data, ensure_ascii=False) + '\n')
        
        elif format.lower() == "csv":
            # Export as CSV
            import csv
            
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                if not self.recent_events:
                    return str(output_path)
                
                # Get field names from first event
                fieldnames = ['timestamp', 'event_type', 'severity', 'actor', 
                             'resource', 'action', 'result', 'session_id', 'run_id', 
                             'plan_id', 'tool_name']
                
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                
                for event in self.recent_events:
                    row = event.to_dict()
                    # Remove details for CSV simplicity
                    row.pop('details', None)
                    # Keep only basic fields
                    row = {k: v for k, v in row.items() if k in fieldnames}
                    writer.writerow(row)
        
        else:
            raise ValueError(f"Unsupported export format: {format}")
        
        return str(output_path)


# Global audit logger instance
_audit_logger = None

def get_audit_logger() -> AuditLogger:
    """Get global audit logger instance"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger


def log_permission_check(tool_name: str, resource: str, action: str, allowed: bool, **kwargs) -> None:
    """Convenience function for logging permission checks"""
    get_audit_logger().log_permission_check(tool_name, resource, action, allowed, **kwargs)


def log_secret_detection(file_path: str, secret_type: str, line_number: int, confidence: float, redacted: bool = True) -> None:
    """Convenience function for logging secret detections"""
    get_audit_logger().log_secret_detection(file_path, secret_type, line_number, confidence, redacted)


def log_tool_execution(tool_name: str, resource: str, success: bool, **kwargs) -> None:
    """Convenience function for logging tool executions"""
    get_audit_logger().log_tool_execution(tool_name, resource, success, **kwargs)


def log_sandbox_violation(tool_name: str, violated_resource: str, violation_type: str, blocked: bool = True) -> None:
    """Convenience function for logging sandbox violations"""
    get_audit_logger().log_sandbox_violation(tool_name, violated_resource, violation_type, blocked)


def log_plan_execution(plan_id: str, action: str, result: str, step_count: int = 0, duration_ms: int = 0, **kwargs) -> None:
    """Convenience function for logging plan execution"""
    get_audit_logger().log_plan_execution(plan_id, action, result, step_count, duration_ms)


def log_approval_event(plan_id: str, step_id: str, approval_action: str, tool_name: str, risk_level: str, **kwargs) -> None:
    """Convenience function for logging approval events"""
    get_audit_logger().log_approval_event(plan_id, step_id, approval_action, tool_name, risk_level)