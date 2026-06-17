"""Data model classes for execution plans. Extracted from plan_manager.py."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from ._deps import all_dependencies_met
from ..tools.base import ToolResult


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
                "success": self.result.is_success,
                "status": self.result.status.value,
                "output_preview": str(self.result.data)[:200] if self.result.data is not None else "",
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
            if all_dependencies_met(
                step.depends_on,
                self.get_step,
                lambda dep: dep.status == StepStatus.COMPLETED,
            ):
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
