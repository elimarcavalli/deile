"""
Approval System for DEILE v4.0
==============================

Manages approval workflows for high-risk operations with timeout,
escalation and audit trail capabilities.

Author: DEILE
Version: 4.0
"""

import logging
import asyncio
import time
import uuid
import json
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Union
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)

class ApprovalStatus(Enum):
    """Status of approval request"""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

class RiskLevel(Enum):
    """Risk levels for operations requiring approval"""
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

@dataclass
class ApprovalRequest:
    """Approval request for high-risk operations"""
    request_id: str
    step_id: str
    plan_id: str
    tool_name: str
    operation: str
    risk_level: RiskLevel
    
    # Request details
    description: str
    consequences: List[str] = field(default_factory=list)
    rollback_available: bool = False
    rollback_description: Optional[str] = None
    
    # Timing
    created_at: float = field(default_factory=time.time)
    timeout: float = 300.0  # 5 minutes default
    expires_at: Optional[float] = None
    
    # Status tracking
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: Optional[str] = None
    denied_by: Optional[str] = None
    approved_at: Optional[float] = None
    denial_reason: Optional[str] = None
    
    # Context and metadata
    context: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.expires_at is None:
            self.expires_at = self.created_at + self.timeout
    
    @property
    def is_expired(self) -> bool:
        """Check if request has expired"""
        return time.time() > self.expires_at
    
    @property
    def time_remaining(self) -> float:
        """Get remaining time before expiry"""
        return max(0, self.expires_at - time.time())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        data = asdict(self)
        data['risk_level'] = self.risk_level.value
        data['status'] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ApprovalRequest':
        """Create from dictionary"""
        data['risk_level'] = RiskLevel(data['risk_level'])
        data['status'] = ApprovalStatus(data['status'])
        return cls(**data)

@dataclass
class ApprovalRule:
    """Rule for automatic approval decisions"""
    rule_id: str
    name: str
    description: str
    
    # Matching criteria
    tool_patterns: List[str] = field(default_factory=list)
    risk_levels: List[RiskLevel] = field(default_factory=list)
    operation_patterns: List[str] = field(default_factory=list)
    
    # Decision
    auto_approve: bool = False
    auto_deny: bool = False
    require_manual: bool = True
    
    # Conditions
    enabled: bool = True
    priority: int = 100
    
    def matches(self, request: ApprovalRequest) -> bool:
        """Check if rule matches approval request"""
        import re
        
        # Check tool patterns
        if self.tool_patterns:
            tool_match = any(
                re.search(pattern, request.tool_name, re.IGNORECASE) 
                for pattern in self.tool_patterns
            )
            if not tool_match:
                return False
        
        # Check risk levels
        if self.risk_levels and request.risk_level not in self.risk_levels:
            return False
        
        # Check operation patterns
        if self.operation_patterns:
            op_match = any(
                re.search(pattern, request.operation, re.IGNORECASE)
                for pattern in self.operation_patterns
            )
            if not op_match:
                return False
        
        return True

class ApprovalSystem:
    """Approval workflow management system"""
    
    def __init__(self, approvals_dir: Path = None):
        """Initialize approval system"""
        self.approvals_dir = approvals_dir or Path("APPROVALS")
        self.approvals_dir.mkdir(exist_ok=True)
        
        # Active requests
        self.pending_requests: Dict[str, ApprovalRequest] = {}
        self.request_futures: Dict[str, asyncio.Future] = {}
        
        # Rules and handlers
        self.approval_rules: List[ApprovalRule] = []
        self.approval_handlers: List[Callable] = []
        
        # Load default rules
        self._load_default_rules()
        
        # Background task for cleanup
        self._cleanup_task = None
        
    def _load_default_rules(self):
        """Load default approval rules"""
        
        # Auto-approve low risk read operations
        self.approval_rules.append(ApprovalRule(
            rule_id="auto_approve_low_risk_read",
            name="Auto-approve Low Risk Read Operations",
            description="Automatically approve low-risk read-only operations",
            tool_patterns=["read_file", "list_files", "find_in_files"],
            risk_levels=[RiskLevel.LOW],
            auto_approve=True,
            priority=10
        ))
        
        # Auto-deny critical file system operations
        self.approval_rules.append(ApprovalRule(
            rule_id="deny_critical_fs_ops",
            name="Deny Critical File System Operations", 
            description="Automatically deny critical file system operations",
            operation_patterns=[r"rm -rf", r"format", r"mkfs", r"dd if=.*of="],
            risk_levels=[RiskLevel.CRITICAL],
            auto_deny=True,
            priority=1
        ))
        
        # Require manual approval for bash execution
        self.approval_rules.append(ApprovalRule(
            rule_id="manual_bash_approval",
            name="Manual Approval for Bash Execution",
            description="Require manual approval for bash command execution",
            tool_patterns=["bash_execute"],
            risk_levels=[RiskLevel.MODERATE, RiskLevel.HIGH, RiskLevel.CRITICAL],
            require_manual=True,
            priority=5
        ))
    
    def add_approval_handler(self, handler: Callable[[ApprovalRequest], None]):
        """Add handler for approval notifications"""
        self.approval_handlers.append(handler)
    
    async def request_approval(self,
                              step_id: str,
                              plan_id: str,
                              tool_name: str,
                              operation: str,
                              risk_level: Union[str, RiskLevel],
                              description: str,
                              consequences: List[str] = None,
                              rollback_available: bool = False,
                              rollback_description: str = None,
                              timeout: float = 300.0,
                              context: Dict[str, Any] = None) -> str:
        """Request approval for operation"""
        
        if isinstance(risk_level, str):
            risk_level = RiskLevel(risk_level.lower())
        
        request_id = f"APPROVAL_{int(time.time())}_{str(uuid.uuid4())[:8]}"
        
        request = ApprovalRequest(
            request_id=request_id,
            step_id=step_id,
            plan_id=plan_id,
            tool_name=tool_name,
            operation=operation,
            risk_level=risk_level,
            description=description,
            consequences=consequences or [],
            rollback_available=rollback_available,
            rollback_description=rollback_description,
            timeout=timeout,
            context=context or {}
        )
        
        # Check rules for auto-decisions
        auto_decision = self._check_rules(request)
        
        if auto_decision == "approved":
            request.status = ApprovalStatus.APPROVED
            request.approved_by = "system_rule"
            request.approved_at = time.time()
            
            logger.info(f"Auto-approved request {request_id} by rule")
            await self._save_request(request)
            return request_id
            
        elif auto_decision == "denied":
            request.status = ApprovalStatus.DENIED
            request.denied_by = "system_rule"
            request.denial_reason = "Denied by automatic rule"
            
            logger.info(f"Auto-denied request {request_id} by rule")
            await self._save_request(request)
            return request_id
        
        # Manual approval required
        self.pending_requests[request_id] = request
        self.request_futures[request_id] = asyncio.Future()
        
        # Save request
        await self._save_request(request)
        
        # Notify handlers
        for handler in self.approval_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(request)
                else:
                    handler(request)
            except Exception as e:
                logger.warning(f"Approval handler error: {e}")
        
        logger.info(f"Created approval request {request_id} for {tool_name}")
        return request_id
    
    async def wait_for_approval(self, request_id: str) -> bool:
        """Wait for approval decision"""
        
        request = self.pending_requests.get(request_id)
        if not request:
            # Check if completed request exists
            request = await self._load_request(request_id)
            if request and request.status in [ApprovalStatus.APPROVED, ApprovalStatus.DENIED]:
                return request.status == ApprovalStatus.APPROVED
            return False
        
        # Check if already decided
        if request.status != ApprovalStatus.PENDING:
            return request.status == ApprovalStatus.APPROVED
        
        try:
            # Wait for decision with timeout
            await asyncio.wait_for(
                self.request_futures[request_id],
                timeout=request.time_remaining
            )
            
            # Check final status
            return request.status == ApprovalStatus.APPROVED
            
        except asyncio.TimeoutError:
            # Request timed out
            request.status = ApprovalStatus.TIMEOUT
            await self._save_request(request)
            
            # Cleanup
            self.pending_requests.pop(request_id, None)
            future = self.request_futures.pop(request_id, None)
            if future and not future.done():
                future.cancel()
            
            logger.warning(f"Approval request {request_id} timed out")
            return False
    
    def approve_request(self, 
                       request_id: str,
                       approved_by: str = "user") -> bool:
        """Approve pending request"""
        
        request = self.pending_requests.get(request_id)
        if not request or request.status != ApprovalStatus.PENDING:
            return False
        
        request.status = ApprovalStatus.APPROVED
        request.approved_by = approved_by
        request.approved_at = time.time()
        
        # Complete future
        future = self.request_futures.get(request_id)
        if future and not future.done():
            future.set_result(True)
        
        # Save and cleanup
        asyncio.create_task(self._save_request(request))
        self._cleanup_request(request_id)
        
        logger.info(f"Approved request {request_id} by {approved_by}")
        return True
    
    def deny_request(self, 
                    request_id: str, 
                    denied_by: str = "user",
                    reason: str = None) -> bool:
        """Deny pending request"""
        
        request = self.pending_requests.get(request_id)
        if not request or request.status != ApprovalStatus.PENDING:
            return False
        
        request.status = ApprovalStatus.DENIED
        request.denied_by = denied_by
        request.denial_reason = reason or "Denied by user"
        
        # Complete future
        future = self.request_futures.get(request_id)
        if future and not future.done():
            future.set_result(False)
        
        # Save and cleanup
        asyncio.create_task(self._save_request(request))
        self._cleanup_request(request_id)
        
        logger.info(f"Denied request {request_id} by {denied_by}: {reason}")
        return True
    
    def cancel_request(self, request_id: str) -> bool:
        """Cancel pending request"""
        
        request = self.pending_requests.get(request_id)
        if not request or request.status != ApprovalStatus.PENDING:
            return False
        
        request.status = ApprovalStatus.CANCELLED
        
        # Complete future
        future = self.request_futures.get(request_id)
        if future and not future.done():
            future.cancel()
        
        # Save and cleanup
        asyncio.create_task(self._save_request(request))
        self._cleanup_request(request_id)
        
        logger.info(f"Cancelled request {request_id}")
        return True
    
    def get_pending_requests(self) -> List[ApprovalRequest]:
        """Get all pending approval requests"""
        return list(self.pending_requests.values())
    
    def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        """Get specific approval request"""
        # Check active requests first
        if request_id in self.pending_requests:
            return self.pending_requests[request_id]
        
        # Load from storage
        return asyncio.create_task(self._load_request(request_id))
    
    async def list_requests(self,
                           status_filter: Optional[ApprovalStatus] = None,
                           plan_id_filter: Optional[str] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        """List approval requests with filtering"""
        
        requests = []
        
        # Add pending requests
        for request in self.pending_requests.values():
            if status_filter and request.status != status_filter:
                continue
            if plan_id_filter and request.plan_id != plan_id_filter:
                continue
            
            requests.append(self._request_to_summary(request))
        
        # Add completed requests from storage
        try:
            for request_file in self.approvals_dir.glob("*.json"):
                if len(requests) >= limit:
                    break
                
                with open(request_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Skip if already in pending
                if data.get('request_id') in self.pending_requests:
                    continue
                
                if status_filter and data.get('status') != status_filter.value:
                    continue
                if plan_id_filter and data.get('plan_id') != plan_id_filter:
                    continue
                
                requests.append({
                    "request_id": data.get("request_id"),
                    "plan_id": data.get("plan_id"),
                    "step_id": data.get("step_id"),
                    "tool_name": data.get("tool_name"),
                    "operation": data.get("operation"),
                    "risk_level": data.get("risk_level"),
                    "status": data.get("status"),
                    "created_at": data.get("created_at"),
                    "approved_at": data.get("approved_at"),
                    "approved_by": data.get("approved_by"),
                    "denied_by": data.get("denied_by"),
                    "denial_reason": data.get("denial_reason")
                })
                
        except Exception as e:
            logger.warning(f"Error reading approval requests: {e}")
        
        # Sort by creation time (most recent first)
        requests.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        
        return requests[:limit]
    
    def _check_rules(self, request: ApprovalRequest) -> Optional[str]:
        """Check approval rules for auto-decision"""
        
        # Sort rules by priority (lower number = higher priority)
        sorted_rules = sorted(
            [rule for rule in self.approval_rules if rule.enabled],
            key=lambda r: r.priority
        )
        
        for rule in sorted_rules:
            if rule.matches(request):
                if rule.auto_approve:
                    logger.debug(f"Rule {rule.rule_id} auto-approved request")
                    return "approved"
                elif rule.auto_deny:
                    logger.debug(f"Rule {rule.rule_id} auto-denied request")
                    return "denied"
                elif rule.require_manual:
                    logger.debug(f"Rule {rule.rule_id} requires manual approval")
                    break
        
        return None  # Manual approval required
    
    def _request_to_summary(self, request: ApprovalRequest) -> Dict[str, Any]:
        """Convert request to summary dict"""
        return {
            "request_id": request.request_id,
            "plan_id": request.plan_id,
            "step_id": request.step_id,
            "tool_name": request.tool_name,
            "operation": request.operation,
            "risk_level": request.risk_level.value,
            "status": request.status.value,
            "description": request.description,
            "created_at": request.created_at,
            "expires_at": request.expires_at,
            "time_remaining": request.time_remaining,
            "approved_at": request.approved_at,
            "approved_by": request.approved_by,
            "denied_by": request.denied_by,
            "denial_reason": request.denial_reason,
            "rollback_available": request.rollback_available
        }
    
    def _cleanup_request(self, request_id: str):
        """Clean up request from memory"""
        self.pending_requests.pop(request_id, None)
        future = self.request_futures.pop(request_id, None)
        if future and not future.done():
            future.cancel()
    
    async def _save_request(self, request: ApprovalRequest):
        """Save request to storage"""
        
        request_file = self.approvals_dir / f"{request.request_id}.json"
        
        try:
            with open(request_file, 'w', encoding='utf-8') as f:
                json.dump(request.to_dict(), f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.error(f"Failed to save approval request {request.request_id}: {e}")
            raise
    
    async def _load_request(self, request_id: str) -> Optional[ApprovalRequest]:
        """Load request from storage"""
        
        request_file = self.approvals_dir / f"{request_id}.json"
        if not request_file.exists():
            return None
        
        try:
            with open(request_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return ApprovalRequest.from_dict(data)
            
        except Exception as e:
            logger.error(f"Failed to load approval request {request_id}: {e}")
            return None
    
    async def start_cleanup_task(self):
        """Start background cleanup task"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_expired_requests())
    
    async def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def _cleanup_expired_requests(self):
        """Background task to cleanup expired requests"""
        
        while True:
            try:
                current_time = time.time()
                expired_requests = []
                
                # Find expired requests
                for request_id, request in self.pending_requests.items():
                    if request.is_expired:
                        expired_requests.append(request_id)
                
                # Timeout expired requests
                for request_id in expired_requests:
                    request = self.pending_requests[request_id]
                    request.status = ApprovalStatus.TIMEOUT
                    
                    # Complete future
                    future = self.request_futures.get(request_id)
                    if future and not future.done():
                        future.set_result(False)
                    
                    await self._save_request(request)
                    self._cleanup_request(request_id)
                    
                    logger.info(f"Auto-expired approval request {request_id}")
                
                # Sleep for cleanup interval
                await asyncio.sleep(60)  # Check every minute
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
                await asyncio.sleep(60)


# Global instance
_approval_system: Optional[ApprovalSystem] = None

def get_approval_system() -> ApprovalSystem:
    """Get singleton instance of ApprovalSystem"""
    global _approval_system
    if _approval_system is None:
        _approval_system = ApprovalSystem()
    return _approval_system