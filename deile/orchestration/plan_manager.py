"""Plan Manager - Sistema de orquestração autônoma com plans e execução"""

from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timedelta
import json
import asyncio
import uuid
import logging
from pathlib import Path

from ..core.exceptions import DEILEError
from ..tools.registry import get_tool_registry
from ..tools.base import ToolResult, ToolStatus
from ..security import (
    get_permission_manager, get_audit_logger, AuditEventType, SeverityLevel,
    log_plan_execution, log_tool_execution, log_permission_check
)

logger = logging.getLogger(__name__)


class PlanStatus(Enum):
    """Status de um plano"""
    DRAFT = "draft"
    READY = "ready" 
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(Enum):
    """Status de um step do plano"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REQUIRES_APPROVAL = "requires_approval"


class RiskLevel(Enum):
    """Nível de risco de um step"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PlanStep:
    """Representa um step de execução em um plano"""
    id: str
    tool_name: str
    params: Dict[str, Any]
    description: str = ""
    expected_output: Optional[str] = None
    rollback: Optional[Dict[str, Any]] = None
    risk_level: RiskLevel = RiskLevel.LOW
    timeout: int = 300  # 5 minutos default
    requires_approval: bool = False
    depends_on: List[str] = field(default_factory=list)
    
    # Status de execução
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[ToolResult] = None
    retry_count: int = 0
    max_retries: int = 3
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict serializável"""
        data = asdict(self)
        # Converte enums para string
        data["risk_level"] = self.risk_level.value
        data["status"] = self.status.value
        # Converte datetime para ISO string
        if self.started_at:
            data["started_at"] = self.started_at.isoformat()
        if self.completed_at:
            data["completed_at"] = self.completed_at.isoformat()
        # Remove result complexo - será salvo separadamente
        if self.result:
            data["result"] = {
                "success": self.result.success,
                "status": self.result.status.value,
                "output_preview": str(self.result.output)[:200],
                "artifact_path": self.result.artifact_path
            }
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PlanStep':
        """Cria instância a partir de dict"""
        # Converte strings de volta para enums
        if "risk_level" in data:
            data["risk_level"] = RiskLevel(data["risk_level"])
        if "status" in data:
            data["status"] = StepStatus(data["status"])
        # Converte datetime strings
        if "started_at" in data and data["started_at"]:
            data["started_at"] = datetime.fromisoformat(data["started_at"])
        if "completed_at" in data and data["completed_at"]:
            data["completed_at"] = datetime.fromisoformat(data["completed_at"])
        # Remove result - será carregado separadamente se necessário
        if "result" in data:
            del data["result"]
        
        return cls(**data)


@dataclass
class ExecutionPlan:
    """Representa um plano de execução completo"""
    id: str
    title: str
    description: str
    created_at: datetime
    created_by: str = "user"
    
    # Steps do plano
    steps: List[PlanStep] = field(default_factory=list)
    
    # Metadados de execução
    status: PlanStatus = PlanStatus.DRAFT
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    estimated_duration: Optional[timedelta] = None
    actual_duration: Optional[timedelta] = None
    
    # Configurações
    max_concurrent_steps: int = 1
    stop_on_failure: bool = True
    require_approval_for_high_risk: bool = True
    
    # Estatísticas
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    
    # Contexto e metadados
    context: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if self.total_steps == 0:
            self.total_steps = len(self.steps)
    
    def add_step(self, step: PlanStep) -> None:
        """Adiciona um step ao plano"""
        self.steps.append(step)
        self.total_steps = len(self.steps)
    
    def get_step(self, step_id: str) -> Optional[PlanStep]:
        """Obtém um step pelo ID"""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None
    
    def get_next_steps(self) -> List[PlanStep]:
        """Obtém próximos steps prontos para execução"""
        ready_steps = []
        
        for step in self.steps:
            if step.status != StepStatus.PENDING:
                continue
            
            # Verifica dependências
            dependencies_met = True
            for dep_id in step.depends_on:
                dep_step = self.get_step(dep_id)
                if not dep_step or dep_step.status != StepStatus.COMPLETED:
                    dependencies_met = False
                    break
            
            if dependencies_met:
                ready_steps.append(step)
        
        return ready_steps
    
    def update_stats(self) -> None:
        """Atualiza estatísticas do plano"""
        self.completed_steps = sum(1 for s in self.steps if s.status == StepStatus.COMPLETED)
        self.failed_steps = sum(1 for s in self.steps if s.status == StepStatus.FAILED)
        self.skipped_steps = sum(1 for s in self.steps if s.status == StepStatus.SKIPPED)
        
        # Atualiza duração se necessário
        if self.started_at and self.completed_at:
            self.actual_duration = self.completed_at - self.started_at
    
    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict serializável"""
        data = asdict(self)
        data["status"] = self.status.value
        data["created_at"] = self.created_at.isoformat()
        
        if self.started_at:
            data["started_at"] = self.started_at.isoformat()
        if self.completed_at:
            data["completed_at"] = self.completed_at.isoformat()
        if self.estimated_duration:
            data["estimated_duration"] = self.estimated_duration.total_seconds()
        if self.actual_duration:
            data["actual_duration"] = self.actual_duration.total_seconds()
        
        # Converte steps
        data["steps"] = [step.to_dict() for step in self.steps]
        
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ExecutionPlan':
        """Cria instância a partir de dict"""
        # Converte enums e datetime
        data["status"] = PlanStatus(data["status"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        
        if "started_at" in data and data["started_at"]:
            data["started_at"] = datetime.fromisoformat(data["started_at"])
        if "completed_at" in data and data["completed_at"]:
            data["completed_at"] = datetime.fromisoformat(data["completed_at"])
        if "estimated_duration" in data and data["estimated_duration"]:
            data["estimated_duration"] = timedelta(seconds=data["estimated_duration"])
        if "actual_duration" in data and data["actual_duration"]:
            data["actual_duration"] = timedelta(seconds=data["actual_duration"])
        
        # Converte steps
        steps_data = data.pop("steps", [])
        plan = cls(**data)
        plan.steps = [PlanStep.from_dict(step_data) for step_data in steps_data]
        plan.total_steps = len(plan.steps)
        
        return plan


class PlanManager:
    """Gerenciador de planos e execução autônoma"""
    
    def __init__(self, plans_dir: str = "./PLANS", runs_dir: str = "./RUNS"):
        self.plans_dir = Path(plans_dir)
        self.runs_dir = Path(runs_dir)
        self.plans_dir.mkdir(exist_ok=True)
        self.runs_dir.mkdir(exist_ok=True)
        
        self._active_plans: Dict[str, ExecutionPlan] = {}
        self._execution_locks: Dict[str, asyncio.Lock] = {}
        self._stop_flags: Dict[str, bool] = {}
        
        self.tool_registry = get_tool_registry()
        
        # Security components
        self.permission_manager = get_permission_manager()
        self.audit_logger = get_audit_logger()
    
    async def create_plan(self, title: str, description: str, 
                         objective: str, context: Optional[Dict[str, Any]] = None) -> ExecutionPlan:
        """Cria um novo plano baseado em um objetivo"""
        
        plan_id = str(uuid.uuid4())[:8]
        
        plan = ExecutionPlan(
            id=plan_id,
            title=title,
            description=description,
            created_at=datetime.now(),
            context=context or {}
        )
        
        # Analisa objetivo e cria steps
        steps = await self._generate_steps_from_objective(objective, context or {})
        
        for step in steps:
            plan.add_step(step)
        
        # Calcula duração estimada
        plan.estimated_duration = timedelta(seconds=sum(step.timeout for step in steps))
        
        # Salva plano
        await self._save_plan(plan)
        
        logger.info(f"Created plan {plan_id} with {len(steps)} steps")
        return plan
    
    async def load_plan(self, plan_id: str) -> Optional[ExecutionPlan]:
        """Carrega um plano salvo"""
        
        plan_file = self.plans_dir / f"{plan_id}.json"
        if not plan_file.exists():
            return None
        
        try:
            with open(plan_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            plan = ExecutionPlan.from_dict(data)
            return plan
            
        except Exception as e:
            logger.error(f"Failed to load plan {plan_id}: {e}")
            return None
    
    async def list_plans(self, status_filter: Optional[PlanStatus] = None) -> List[Dict[str, Any]]:
        """Lista planos disponíveis"""
        
        plans_info = []
        
        for plan_file in self.plans_dir.glob("*.json"):
            try:
                with open(plan_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Filtra por status se necessário
                if status_filter and data.get("status") != status_filter.value:
                    continue
                
                # Informações resumidas
                plans_info.append({
                    "id": data["id"],
                    "title": data["title"],
                    "description": data["description"],
                    "status": data["status"],
                    "created_at": data["created_at"],
                    "total_steps": data["total_steps"],
                    "completed_steps": data["completed_steps"],
                    "failed_steps": data["failed_steps"]
                })
                
            except Exception as e:
                logger.warning(f"Failed to read plan {plan_file}: {e}")
        
        # Ordena por data de criação (mais recente primeiro)
        plans_info.sort(key=lambda x: x["created_at"], reverse=True)
        
        return plans_info
    
    async def execute_plan(self, plan_id: str, 
                          auto_approve_low_risk: bool = True) -> Dict[str, Any]:
        """Executa um plano"""
        
        # Carrega plano
        plan = await self.load_plan(plan_id)
        if not plan:
            raise DEILEError(f"Plan {plan_id} not found", error_code="PLAN_NOT_FOUND")
        
        if plan.status not in [PlanStatus.READY, PlanStatus.DRAFT, PlanStatus.PAUSED]:
            raise DEILEError(f"Plan {plan_id} cannot be executed (status: {plan.status})", 
                           error_code="PLAN_NOT_EXECUTABLE")
        
        # Configura execução
        self._active_plans[plan_id] = plan
        self._execution_locks[plan_id] = asyncio.Lock()
        self._stop_flags[plan_id] = False
        
        try:
            # Log início da execução do plano
            self.audit_logger.log_plan_execution(
                plan_id=plan_id,
                action="start",
                result="initiated",
                step_count=len(plan.steps)
            )
            
            # Inicia execução
            plan.status = PlanStatus.RUNNING
            plan.started_at = datetime.now()
            await self._save_plan(plan)
            
            logger.info(f"Starting execution of plan {plan_id}")
            
            # Executa steps
            execution_summary = await self._execute_plan_steps(
                plan, auto_approve_low_risk
            )
            
            # Finaliza plano
            plan.completed_at = datetime.now()
            plan.update_stats()
            
            if plan.failed_steps > 0 and plan.stop_on_failure:
                plan.status = PlanStatus.FAILED
            else:
                plan.status = PlanStatus.COMPLETED
            
            await self._save_plan(plan)
            
            # Gera relatório final
            execution_summary["plan_summary"] = {
                "id": plan.id,
                "title": plan.title,
                "status": plan.status.value,
                "total_steps": plan.total_steps,
                "completed_steps": plan.completed_steps,
                "failed_steps": plan.failed_steps,
                "duration": plan.actual_duration.total_seconds() if plan.actual_duration else 0
            }
            
            # Log finalização do plano
            duration_ms = int(plan.actual_duration.total_seconds() * 1000) if plan.actual_duration else 0
            
            self.audit_logger.log_plan_execution(
                plan_id=plan_id,
                action="complete",
                result=plan.status.value,
                step_count=plan.total_steps,
                duration_ms=duration_ms
            )
            
            logger.info(f"Completed execution of plan {plan_id}: {plan.status}")
            return execution_summary
            
        except Exception as e:
            plan.status = PlanStatus.FAILED
            plan.completed_at = datetime.now()
            await self._save_plan(plan)
            
            # Log falha do plano
            self.audit_logger.log_plan_execution(
                plan_id=plan_id,
                action="fail",
                result="error",
                step_count=len(plan.steps)
            )
            
            logger.error(f"Failed to execute plan {plan_id}: {e}")
            raise
        
        finally:
            # Cleanup
            self._active_plans.pop(plan_id, None)
            self._execution_locks.pop(plan_id, None)
            self._stop_flags.pop(plan_id, None)
    
    async def stop_plan(self, plan_id: str) -> bool:
        """Para a execução de um plano"""
        
        if plan_id not in self._active_plans:
            return False
        
        self._stop_flags[plan_id] = True
        
        plan = self._active_plans[plan_id]
        plan.status = PlanStatus.CANCELLED
        await self._save_plan(plan)
        
        logger.info(f"Stopped execution of plan {plan_id}")
        return True
    
    async def approve_step(self, plan_id: str, step_id: str, approved: bool = True) -> bool:
        """Aprova ou rejeita um step que requer aprovação"""
        
        plan = self._active_plans.get(plan_id)
        if not plan:
            return False
        
        step = plan.get_step(step_id)
        if not step or step.status != StepStatus.REQUIRES_APPROVAL:
            return False
        
        if approved:
            step.status = StepStatus.PENDING
            action = "granted"
            logger.info(f"Approved step {step_id} in plan {plan_id}")
        else:
            step.status = StepStatus.SKIPPED
            action = "denied"
            logger.info(f"Rejected step {step_id} in plan {plan_id}")
        
        # Log evento de aprovação
        self.audit_logger.log_approval_event(
            plan_id=plan_id,
            step_id=step_id,
            approval_action=action,
            tool_name=step.tool_name,
            risk_level=step.risk_level.value
        )
        
        await self._save_plan(plan)
        return True
    
    async def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Obtém status detalhado de um plano"""
        
        # Tenta plano ativo primeiro
        plan = self._active_plans.get(plan_id)
        if not plan:
            plan = await self.load_plan(plan_id)
        
        if not plan:
            return None
        
        return {
            "id": plan.id,
            "title": plan.title,
            "status": plan.status.value,
            "progress": {
                "total_steps": plan.total_steps,
                "completed": plan.completed_steps,
                "failed": plan.failed_steps,
                "skipped": plan.skipped_steps,
                "percentage": (plan.completed_steps / plan.total_steps * 100) if plan.total_steps > 0 else 0
            },
            "timing": {
                "created_at": plan.created_at.isoformat(),
                "started_at": plan.started_at.isoformat() if plan.started_at else None,
                "completed_at": plan.completed_at.isoformat() if plan.completed_at else None,
                "estimated_duration": plan.estimated_duration.total_seconds() if plan.estimated_duration else None,
                "actual_duration": plan.actual_duration.total_seconds() if plan.actual_duration else None
            },
            "current_steps": [
                {
                    "id": step.id,
                    "description": step.description,
                    "status": step.status.value,
                    "requires_approval": step.requires_approval
                }
                for step in plan.steps 
                if step.status in [StepStatus.RUNNING, StepStatus.REQUIRES_APPROVAL]
            ]
        }
    
    async def _generate_steps_from_objective(self, objective: str, 
                                           context: Dict[str, Any]) -> List[PlanStep]:
        """Gera steps baseado no objetivo (versão simplificada - mockup)"""
        
        # Esta é uma implementação mockup. Em produção, usaria LLM para gerar steps
        steps = []
        
        # Análise básica do objetivo para determinar steps
        if "file" in objective.lower() or "read" in objective.lower():
            steps.append(PlanStep(
                id=str(uuid.uuid4())[:8],
                tool_name="read_file",
                params={"path": context.get("target_file", "README.md")},
                description="Read target file",
                risk_level=RiskLevel.LOW,
                timeout=30
            ))
        
        if "list" in objective.lower() or "directory" in objective.lower():
            steps.append(PlanStep(
                id=str(uuid.uuid4())[:8],
                tool_name="list_files", 
                params={"path": context.get("target_dir", "."), "recursive": True},
                description="List files in directory",
                risk_level=RiskLevel.LOW,
                timeout=60
            ))
        
        if "search" in objective.lower() or "find" in objective.lower():
            steps.append(PlanStep(
                id=str(uuid.uuid4())[:8],
                tool_name="find_in_files",
                params={
                    "pattern": context.get("search_pattern", "TODO"),
                    "path": context.get("search_path", "."),
                    "max_context_lines": 5
                },
                description="Search for pattern in files",
                risk_level=RiskLevel.LOW,
                timeout=120
            ))
        
        if "run" in objective.lower() or "execute" in objective.lower():
            steps.append(PlanStep(
                id=str(uuid.uuid4())[:8],
                tool_name="bash_execute",
                params={
                    "command": context.get("command", "echo 'Hello World'"),
                    "show_cli": True,
                    "security_level": "safe"
                },
                description="Execute command",
                risk_level=RiskLevel.MEDIUM,
                timeout=300,
                requires_approval=True
            ))
        
        # Se nenhum step específico foi gerado, cria um step genérico
        if not steps:
            steps.append(PlanStep(
                id=str(uuid.uuid4())[:8],
                tool_name="list_files",
                params={"path": ".", "recursive": False},
                description=f"General analysis for: {objective}",
                risk_level=RiskLevel.LOW,
                timeout=60
            ))
        
        return steps
    
    async def _execute_plan_steps(self, plan: ExecutionPlan, 
                                auto_approve_low_risk: bool) -> Dict[str, Any]:
        """Executa os steps de um plano"""
        
        execution_log = []
        
        while True:
            # Verifica flag de parada
            if self._stop_flags.get(plan.id, False):
                break
            
            # Obtém próximos steps
            ready_steps = plan.get_next_steps()
            if not ready_steps:
                # Verifica se há steps aguardando aprovação
                approval_steps = [s for s in plan.steps if s.status == StepStatus.REQUIRES_APPROVAL]
                if approval_steps:
                    execution_log.append({
                        "action": "waiting_approval",
                        "steps": [s.id for s in approval_steps],
                        "timestamp": datetime.now().isoformat()
                    })
                    # Aguarda aprovação (em implementação real, seria um mecanismo de notificação)
                    await asyncio.sleep(1)
                    continue
                else:
                    # Não há mais steps para executar
                    break
            
            # Executa steps (respeitando max_concurrent_steps)
            concurrent_steps = ready_steps[:plan.max_concurrent_steps]
            
            for step in concurrent_steps:
                try:
                    # Verifica se precisa de aprovação
                    if step.requires_approval and step.risk_level not in [RiskLevel.LOW]:
                        if not auto_approve_low_risk or step.risk_level != RiskLevel.LOW:
                            step.status = StepStatus.REQUIRES_APPROVAL
                            continue
                    
                    # Executa step
                    step.status = StepStatus.RUNNING
                    step.started_at = datetime.now()
                    
                    result = await self._execute_step(step)
                    
                    step.result = result
                    step.completed_at = datetime.now()
                    
                    if result.success:
                        step.status = StepStatus.COMPLETED
                        execution_log.append({
                            "step_id": step.id,
                            "action": "completed",
                            "duration": (step.completed_at - step.started_at).total_seconds(),
                            "timestamp": step.completed_at.isoformat()
                        })
                    else:
                        step.status = StepStatus.FAILED
                        step.error_message = result.error_message
                        execution_log.append({
                            "step_id": step.id,
                            "action": "failed", 
                            "error": result.error_message,
                            "timestamp": step.completed_at.isoformat()
                        })
                        
                        # Para execução se configurado
                        if plan.stop_on_failure:
                            break
                
                except Exception as e:
                    step.status = StepStatus.FAILED
                    step.error_message = str(e)
                    step.completed_at = datetime.now()
                    
                    execution_log.append({
                        "step_id": step.id,
                        "action": "error",
                        "error": str(e),
                        "timestamp": step.completed_at.isoformat()
                    })
                    
                    if plan.stop_on_failure:
                        break
            
            # Atualiza estatísticas
            plan.update_stats()
            await self._save_plan(plan)
            
            # Small delay entre iterations
            await asyncio.sleep(0.1)
        
        return {
            "execution_log": execution_log,
            "final_stats": {
                "completed": plan.completed_steps,
                "failed": plan.failed_steps,
                "skipped": plan.skipped_steps
            }
        }
    
    async def _execute_step(self, step: PlanStep) -> ToolResult:
        """Executa um step individual com verificações de segurança"""
        
        start_time = datetime.now()
        
        try:
            # Log início da execução
            self.audit_logger.log_tool_execution(
                tool_name=step.tool_name,
                resource=step.description or f"step_{step.id}",
                success=False,  # Will be updated on completion
                duration_ms=0
            )
            
            # Obtém tool do registry
            tool = self.tool_registry.get_enabled(step.tool_name)
            if not tool:
                # Log erro de tool não encontrada
                self.audit_logger.log_event(
                    event_type=AuditEventType.TOOL_EXECUTION,
                    severity=SeverityLevel.ERROR,
                    actor=step.tool_name,
                    resource=step.description or f"step_{step.id}",
                    action="validate",
                    result="tool_not_found",
                    details={"error": "Tool not found or not enabled"}
                )
                return ToolResult.error_result(
                    f"Tool '{step.tool_name}' not found or not enabled",
                    error_code="TOOL_NOT_FOUND"
                )
            
            # Verificações de segurança baseadas nos parâmetros
            security_check_result = await self._perform_security_checks(step)
            if not security_check_result["allowed"]:
                # Log denied permission
                self.audit_logger.log_permission_check(
                    tool_name=step.tool_name,
                    resource=step.description or f"step_{step.id}",
                    action="execute",
                    allowed=False,
                    rule_id=security_check_result.get("rule_id"),
                    additional_details=security_check_result
                )
                return ToolResult.error_result(
                    f"Permission denied: {security_check_result['reason']}",
                    error_code="PERMISSION_DENIED"
                )
            
            # Log permissão concedida
            self.audit_logger.log_permission_check(
                tool_name=step.tool_name,
                resource=step.description or f"step_{step.id}",
                action="execute",
                allowed=True,
                rule_id=security_check_result.get("rule_id"),
                additional_details=security_check_result
            )
            
            # Executa com timeout
            try:
                result = await asyncio.wait_for(
                    self._run_tool_with_params(tool, step.params),
                    timeout=step.timeout
                )
                
                # Calcula duration
                duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
                
                # Log execução bem-sucedida
                self.audit_logger.log_tool_execution(
                    tool_name=step.tool_name,
                    resource=step.description or f"step_{step.id}",
                    success=result.success,
                    duration_ms=duration_ms,
                    exit_code=getattr(result, 'exit_code', None),
                    output_size=len(str(result.output)) if result.output else 0
                )
                
                return result
                
            except asyncio.TimeoutError:
                return ToolResult.error_result(
                    f"Step timed out after {step.timeout} seconds",
                    error_code="STEP_TIMEOUT"
                )
        
        except Exception as e:
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            # Log erro de execução
            self.audit_logger.log_tool_execution(
                tool_name=step.tool_name,
                resource=step.description or f"step_{step.id}",
                success=False,
                duration_ms=duration_ms
            )
            
            return ToolResult.error_result(
                f"Error executing step: {str(e)}",
                error=e,
                error_code="STEP_EXECUTION_ERROR"
            )
    
    async def _perform_security_checks(self, step: PlanStep) -> Dict[str, Any]:
        """Realiza verificações de segurança para um step"""
        
        # Verificações básicas baseadas no tool
        if step.tool_name == "bash_execute":
            # Verificar comando bash
            command = step.params.get("command", "")
            
            # Verificar permissão para executar comando
            allowed = self.permission_manager.check_permission(
                tool_name="bash_execute",
                resource=command,
                action="execute"
            )
            
            if not allowed:
                return {
                    "allowed": False,
                    "reason": f"Permission denied for bash command: {command}",
                    "rule_id": "bash_security_rule"
                }
        
        elif step.tool_name == "write_file":
            # Verificar permissão para escrever arquivo
            file_path = step.params.get("path", "")
            
            allowed = self.permission_manager.check_permission(
                tool_name="write_file",
                resource=file_path,
                action="write"
            )
            
            if not allowed:
                return {
                    "allowed": False,
                    "reason": f"Permission denied for file write: {file_path}",
                    "rule_id": "file_write_rule"
                }
        
        elif step.tool_name == "delete_file":
            # Verificar permissão para deletar arquivo
            file_path = step.params.get("path", "")
            
            allowed = self.permission_manager.check_permission(
                tool_name="delete_file",
                resource=file_path,
                action="delete"
            )
            
            if not allowed:
                return {
                    "allowed": False,
                    "reason": f"Permission denied for file deletion: {file_path}",
                    "rule_id": "file_delete_rule"
                }
        
        # Verificações de nível de risco
        if step.risk_level == RiskLevel.CRITICAL:
            # Operações críticas sempre precisam de aprovação manual
            if step.status != StepStatus.REQUIRES_APPROVAL and not step.requires_approval:
                return {
                    "allowed": False,
                    "reason": "Critical operations require explicit approval",
                    "rule_id": "critical_risk_rule"
                }
        
        # Se chegou até aqui, é permitido
        return {
            "allowed": True,
            "reason": "Security checks passed",
            "rule_id": "default_allow"
        }
    
    async def _run_tool_with_params(self, tool, params: Dict[str, Any]) -> ToolResult:
        """Executa tool com parâmetros"""
        
        # Usa execute_function_call do registry para compatibilidade
        return self.tool_registry.execute_function_call(
            function_name=tool.name,
            arguments=params
        )
    
    async def _save_plan(self, plan: ExecutionPlan) -> None:
        """Salva plano em arquivo JSON"""
        
        plan_file = self.plans_dir / f"{plan.id}.json"
        
        try:
            with open(plan_file, 'w', encoding='utf-8') as f:
                json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)
            
            # Também salva versão human-readable
            md_file = self.plans_dir / f"{plan.id}.md"
            await self._save_plan_markdown(plan, md_file)
            
        except Exception as e:
            logger.error(f"Failed to save plan {plan.id}: {e}")
            raise
    
    async def _save_plan_markdown(self, plan: ExecutionPlan, file_path: Path) -> None:
        """Salva plano em formato markdown legível"""
        
        content = [
            f"# Plan: {plan.title}",
            "",
            f"**ID:** {plan.id}",
            f"**Status:** {plan.status.value}",
            f"**Created:** {plan.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"## Description",
            plan.description,
            "",
            f"## Steps ({len(plan.steps)} total)",
            ""
        ]
        
        for i, step in enumerate(plan.steps, 1):
            status_emoji = {
                StepStatus.PENDING: "⏳",
                StepStatus.RUNNING: "🔄", 
                StepStatus.COMPLETED: "✅",
                StepStatus.FAILED: "❌",
                StepStatus.SKIPPED: "⏭️",
                StepStatus.REQUIRES_APPROVAL: "⚠️"
            }.get(step.status, "❓")
            
            content.extend([
                f"### {i}. {step.description} {status_emoji}",
                "",
                f"- **Tool:** {step.tool_name}",
                f"- **Risk Level:** {step.risk_level.value}",
                f"- **Status:** {step.status.value}",
                f"- **Timeout:** {step.timeout}s"
            ])
            
            if step.requires_approval:
                content.append("- **Requires Approval:** Yes")
            
            if step.depends_on:
                content.append(f"- **Depends on:** {', '.join(step.depends_on)}")
            
            if step.error_message:
                content.extend([
                    "- **Error:**",
                    f"  ```",
                    f"  {step.error_message}",
                    f"  ```"
                ])
            
            content.append("")
        
        # Estatísticas
        if plan.total_steps > 0:
            content.extend([
                "## Statistics",
                "",
                f"- **Progress:** {plan.completed_steps}/{plan.total_steps} ({plan.completed_steps/plan.total_steps*100:.1f}%)",
                f"- **Completed:** {plan.completed_steps}",
                f"- **Failed:** {plan.failed_steps}",
                f"- **Skipped:** {plan.skipped_steps}",
                ""
            ])
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(content))
        except Exception as e:
            logger.warning(f"Failed to save plan markdown {file_path}: {e}")


# Singleton instance
_plan_manager: Optional[PlanManager] = None


def get_plan_manager() -> PlanManager:
    """Retorna a instância singleton do PlanManager"""
    global _plan_manager
    if _plan_manager is None:
        _plan_manager = PlanManager()
    return _plan_manager