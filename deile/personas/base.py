"""Classes base para sistema de personas DEILE 2.0 ULTRA"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set
from enum import Enum
from pydantic import BaseModel, Field, validator
import logging
import time

logger = logging.getLogger(__name__)


class PersonaCapability(Enum):
    """Capacidades que uma persona pode ter"""
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    DEBUGGING = "debugging"
    ARCHITECTURE_DESIGN = "architecture_design"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    OPTIMIZATION = "optimization"
    SECURITY_ANALYSIS = "security_analysis"
    PROJECT_MANAGEMENT = "project_management"
    MENTORING = "mentoring"
    PROBLEM_SOLVING = "problem_solving"
    RESEARCH = "research"


class PersonaStyle(Enum):
    """Estilos de comunicação da persona"""
    FORMAL = "formal"
    CASUAL = "casual"
    TECHNICAL = "technical"
    FRIENDLY = "friendly"
    MENTOR = "mentor"
    EXPERT = "expert"
    COLLABORATIVE = "collaborative"


class PersonaConfig(BaseModel):
    """Configuração de uma persona usando Pydantic para validação"""

    name: str = Field(..., min_length=1, max_length=50)
    description: str = Field(..., min_length=10, max_length=500)
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")

    # Identificação e metadata
    persona_id: str = Field(..., min_length=1, max_length=100)
    author: Optional[str] = Field(None, max_length=100)
    tags: List[str] = Field(default_factory=list)

    # Capacidades e especializações
    capabilities: List[PersonaCapability] = Field(default_factory=list)
    specializations: List[str] = Field(default_factory=list)
    expertise_level: int = Field(default=5, ge=1, le=10)

    # Configuração de comportamento
    communication_style: PersonaStyle = Field(default=PersonaStyle.FRIENDLY)
    formality_level: int = Field(default=5, ge=1, le=10)
    verbosity_level: int = Field(default=5, ge=1, le=10)

    # System instructions e prompts
    system_instruction: str = Field(..., min_length=50)
    greeting_template: Optional[str] = None
    task_approach_template: Optional[str] = None

    # Configurações específicas
    max_context_length: int = Field(default=8000, ge=1000, le=32000)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    use_tools: bool = Field(default=True)
    auto_suggest_improvements: bool = Field(default=True)

    # Metadata de runtime
    created_at: Optional[float] = Field(default_factory=time.time)
    last_modified: Optional[float] = Field(default_factory=time.time)
    usage_count: int = Field(default=0, ge=0)

    @validator('capabilities')
    def validate_capabilities(cls, v):
        """Valida que há pelo menos uma capacidade"""
        if not v:
            raise ValueError("Persona deve ter pelo menos uma capacidade")
        return v

    @validator('system_instruction')
    def validate_system_instruction(cls, v):
        """Valida que a system instruction contém elementos essenciais"""
        required_elements = ['objetivo', 'persona', 'comportamento']
        v_lower = v.lower()

        missing = [elem for elem in required_elements if elem not in v_lower]
        if missing:
            logger.warning(f"System instruction pode estar incompleta. Elementos sugeridos: {missing}")

        return v

    class Config:
        use_enum_values = True
        validate_assignment = True


@dataclass
class PersonaMetrics:
    """Métricas de performance de uma persona"""
    total_interactions: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    average_response_time: float = 0.0
    user_satisfaction_score: float = 0.0
    last_interaction: Optional[float] = None

    @property
    def success_rate(self) -> float:
        """Taxa de sucesso das tarefas"""
        total = self.successful_tasks + self.failed_tasks
        return (self.successful_tasks / total * 100) if total > 0 else 0.0

    def record_interaction(self, success: bool, response_time: float, satisfaction: Optional[float] = None):
        """Registra uma nova interação"""
        self.total_interactions += 1

        if success:
            self.successful_tasks += 1
        else:
            self.failed_tasks += 1

        # Atualiza tempo médio de resposta
        if self.total_interactions == 1:
            self.average_response_time = response_time
        else:
            self.average_response_time = (
                (self.average_response_time * (self.total_interactions - 1) + response_time)
                / self.total_interactions
            )

        # Atualiza satisfação do usuário
        if satisfaction is not None:
            if self.user_satisfaction_score == 0:
                self.user_satisfaction_score = satisfaction
            else:
                self.user_satisfaction_score = (
                    (self.user_satisfaction_score * (self.total_interactions - 1) + satisfaction)
                    / self.total_interactions
                )

        self.last_interaction = time.time()


class BasePersona(ABC):
    """Classe base abstrata para todas as personas do DEILE 2.0 ULTRA

    Implementa o padrão Strategy para personas intercambiáveis com capacidades
    específicas e comportamentos customizáveis.
    """

    def __init__(self, config: PersonaConfig):
        self.config = config
        self.metrics = PersonaMetrics()
        self._is_active = False
        self._context_cache: Dict[str, Any] = {}
        self._last_context_build = 0.0

        logger.info(f"Persona '{self.config.name}' inicializada com {len(self.config.capabilities)} capacidades")

    @property
    def name(self) -> str:
        """Nome da persona"""
        return self.config.name

    @property
    def persona_id(self) -> str:
        """ID único da persona"""
        return self.config.persona_id

    @property
    def capabilities(self) -> List[PersonaCapability]:
        """Capacidades da persona"""
        return self.config.capabilities

    @property
    def is_active(self) -> bool:
        """Verifica se a persona está ativa"""
        return self._is_active

    @property
    def expertise_areas(self) -> List[str]:
        """Áreas de expertise da persona"""
        return self.config.specializations

    def can_handle_task(self, task_type: str, required_capabilities: List[PersonaCapability] = None) -> bool:
        """Verifica se a persona pode lidar com um tipo de tarefa"""
        if required_capabilities:
            return all(cap in self.config.capabilities for cap in required_capabilities)

        # Análise heurística baseada no tipo de tarefa
        task_lower = task_type.lower()
        capability_mapping = {
            'debug': PersonaCapability.DEBUGGING,
            'code': PersonaCapability.CODE_GENERATION,
            'review': PersonaCapability.CODE_REVIEW,
            'architecture': PersonaCapability.ARCHITECTURE_DESIGN,
            'test': PersonaCapability.TESTING,
            'document': PersonaCapability.DOCUMENTATION,
            'optimize': PersonaCapability.OPTIMIZATION,
            'security': PersonaCapability.SECURITY_ANALYSIS,
            'manage': PersonaCapability.PROJECT_MANAGEMENT,
            'research': PersonaCapability.RESEARCH
        }

        for keyword, capability in capability_mapping.items():
            if keyword in task_lower and capability in self.config.capabilities:
                return True

        return False

    def activate(self) -> None:
        """Ativa a persona"""
        self._is_active = True
        logger.debug(f"Persona '{self.name}' ativada")

    def deactivate(self) -> None:
        """Desativa a persona"""
        self._is_active = False
        self._context_cache.clear()
        logger.debug(f"Persona '{self.name}' desativada")

    @abstractmethod
    async def build_system_instruction(self, context: Dict[str, Any] = None) -> str:
        """Constrói a instrução do sistema baseada no contexto

        Args:
            context: Contexto atual da sessão

        Returns:
            str: System instruction personalizada para o contexto
        """
        pass

    @abstractmethod
    async def process_user_input(self, user_input: str, context: Dict[str, Any] = None) -> str:
        """Processa input do usuário aplicando a personalidade da persona

        Args:
            user_input: Input do usuário
            context: Contexto da sessão

        Returns:
            str: Input processado com estilo da persona
        """
        pass

    async def generate_greeting(self, context: Dict[str, Any] = None) -> str:
        """Gera saudação personalizada da persona"""
        if self.config.greeting_template:
            try:
                return self.config.greeting_template.format(**context) if context else self.config.greeting_template
            except KeyError as e:
                logger.warning(f"Template de saudação tem variável indefinida: {e}")

        # Saudação padrão baseada no estilo
        style_greetings = {
            PersonaStyle.FORMAL: f"Olá! Eu sou {self.name}, especialista em {', '.join(self.expertise_areas)}. Como posso ajudá-lo hoje?",
            PersonaStyle.CASUAL: f"E aí! Sou o {self.name}, seu parceiro para {', '.join(self.expertise_areas[:2])}! No que posso te ajudar?",
            PersonaStyle.TECHNICAL: f"Sistema {self.name} online. Especialidades: {', '.join(self.expertise_areas)}. Aguardando input.",
            PersonaStyle.FRIENDLY: f"Oi! 👋 Eu sou {self.name}, adoro trabalhar com {', '.join(self.expertise_areas)}! Vamos criar algo incrível juntos?",
            PersonaStyle.MENTOR: f"Bem-vindo! Sou {self.name}, e estou aqui para te orientar em {', '.join(self.expertise_areas)}. Qual é seu objetivo hoje?",
            PersonaStyle.EXPERT: f"Saudações. {self.name} aqui, com experiência profunda em {', '.join(self.expertise_areas)}. Como posso aplicar minha expertise?",
            PersonaStyle.COLLABORATIVE: f"Olá, parceiro! Sou {self.name}, vamos colaborar em {', '.join(self.expertise_areas)}! Qual é nosso próximo desafio?"
        }

        return style_greetings.get(self.config.communication_style, f"Olá! Sou {self.name}, como posso ajudar?")

    async def validate_config(self) -> List[str]:
        """Valida a configuração da persona

        Returns:
            List[str]: Lista de erros de validação (vazia se válida)
        """
        errors = []

        try:
            # Validação via Pydantic já ocorre no __init__
            # Validações adicionais específicas da persona
            if not self.config.capabilities:
                errors.append("Persona deve ter pelo menos uma capacidade")

            if len(self.config.system_instruction) < 50:
                errors.append("System instruction muito curta (mínimo 50 caracteres)")

            if self.config.expertise_level < 1 or self.config.expertise_level > 10:
                errors.append("Expertise level deve estar entre 1 e 10")

        except Exception as e:
            errors.append(f"Erro na validação: {str(e)}")

        return errors

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas da persona"""
        return {
            "name": self.name,
            "persona_id": self.persona_id,
            "capabilities": [cap.value for cap in self.capabilities],
            "expertise_areas": self.expertise_areas,
            "is_active": self.is_active,
            "config": {
                "communication_style": self.config.communication_style.value,
                "expertise_level": self.config.expertise_level,
                "formality_level": self.config.formality_level,
                "verbosity_level": self.config.verbosity_level
            },
            "metrics": {
                "total_interactions": self.metrics.total_interactions,
                "success_rate": self.metrics.success_rate,
                "average_response_time": self.metrics.average_response_time,
                "user_satisfaction": self.metrics.user_satisfaction_score,
                "last_interaction": self.metrics.last_interaction
            }
        }

    def __str__(self) -> str:
        return f"Persona({self.name}, {len(self.capabilities)} capabilities)"

    def __repr__(self) -> str:
        return f"<Persona: {self.name} [{self.persona_id}]>"