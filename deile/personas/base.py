"""
DEILE Autonomous AI Agent Persona System - ULTRA RIGOROSO
============================================================
Base classes for autonomous AI agents using LLM APIs with function calling.
Implements best practices for production-grade AI agents.

Author: DEILE Team
Version: 3.0.0 ULTRA
License: MIT
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set, Tuple, Callable, Union
from enum import Enum, auto
from pydantic import BaseModel, Field, field_validator, model_validator
import logging
import time
import hashlib
import json
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ============================================================
# CORE ENUMS - Autonomous Agent Capabilities
# ============================================================

class AgentCapability(Enum):
    """Core capabilities for autonomous AI agents"""
    # Code & Development
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    CODE_REFACTORING = "code_refactoring"
    DEBUGGING = "debugging"

    # Architecture & Design
    ARCHITECTURE_DESIGN = "architecture_design"
    SYSTEM_DESIGN = "system_design"
    API_DESIGN = "api_design"
    DATABASE_DESIGN = "database_design"

    # Testing & Quality
    TESTING = "testing"
    TEST_GENERATION = "test_generation"
    PERFORMANCE_TESTING = "performance_testing"
    SECURITY_TESTING = "security_testing"

    # Analysis & Research
    CODE_ANALYSIS = "code_analysis"
    SECURITY_ANALYSIS = "security_analysis"
    PERFORMANCE_ANALYSIS = "performance_analysis"
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    RESEARCH = "research"

    # Documentation & Knowledge
    DOCUMENTATION = "documentation"
    KNOWLEDGE_EXTRACTION = "knowledge_extraction"
    EXPLANATION = "explanation"
    TUTORIAL_CREATION = "tutorial_creation"

    # Optimization & Performance
    OPTIMIZATION = "optimization"
    PERFORMANCE_TUNING = "performance_tuning"
    RESOURCE_OPTIMIZATION = "resource_optimization"
    ALGORITHM_OPTIMIZATION = "algorithm_optimization"

    # Project & Process
    PROJECT_MANAGEMENT = "project_management"
    TASK_PLANNING = "task_planning"
    WORKFLOW_AUTOMATION = "workflow_automation"
    CI_CD_SETUP = "ci_cd_setup"

    # AI & ML Specific
    PROMPT_ENGINEERING = "prompt_engineering"
    MODEL_FINE_TUNING = "model_fine_tuning"
    DATA_PREPARATION = "data_preparation"
    ML_PIPELINE_DESIGN = "ml_pipeline_design"

    # Collaboration & Communication
    MENTORING = "mentoring"
    PAIR_PROGRAMMING = "pair_programming"
    CODE_TRANSLATION = "code_translation"
    TECHNICAL_WRITING = "technical_writing"

    # Problem Solving
    PROBLEM_SOLVING = "problem_solving"
    ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
    SOLUTION_DESIGN = "solution_design"
    TRADE_OFF_ANALYSIS = "trade_off_analysis"


class CommunicationStyle(Enum):
    """Communication styles for agent interactions"""
    ULTRA_TECHNICAL = "ultra_technical"  # Maximum technical depth
    TECHNICAL = "technical"              # Technical but accessible
    BALANCED = "balanced"                # Mix of technical and plain
    EDUCATIONAL = "educational"          # Teaching-focused
    COLLABORATIVE = "collaborative"      # Team-oriented
    MENTOR = "mentor"                    # Guiding and supportive
    CONCISE = "concise"                  # Minimal, to-the-point
    DETAILED = "detailed"                # Comprehensive explanations
    ADAPTIVE = "adaptive"                # Adjusts based on context


class ToolExecutionStrategy(Enum):
    """Strategies for tool execution by the agent"""
    SEQUENTIAL = "sequential"        # Execute tools one by one
    PARALLEL = "parallel"            # Execute independent tools in parallel
    ADAPTIVE = "adaptive"            # Choose based on dependencies
    BATCH = "batch"                  # Group similar operations
    PIPELINE = "pipeline"            # Chain tools in a pipeline
    ORCHESTRATED = "orchestrated"    # Complex multi-tool orchestration


class ResponseMode(Enum):
    """How the agent should structure responses"""
    DIRECT = "direct"                # Direct answer only
    EXPLANATORY = "explanatory"      # Answer with explanation
    STEP_BY_STEP = "step_by_step"    # Detailed steps
    ANALYTICAL = "analytical"        # Deep analysis
    CREATIVE = "creative"            # Creative solutions
    EXPLORATORY = "exploratory"      # Explore multiple options


# ============================================================
# PROMPT ENGINEERING COMPONENTS
# ============================================================

@dataclass
class PromptComponent:
    """Building block for dynamic prompt construction"""
    name: str
    content: str
    priority: int = 5  # 1-10, higher = more important
    conditional: Optional[Callable[[Dict], bool]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def should_include(self, context: Dict[str, Any]) -> bool:
        """Check if this component should be included based on context"""
        if self.conditional:
            return self.conditional(context)
        return True

    def render(self, context: Dict[str, Any]) -> str:
        """Render the component with context variables"""
        try:
            return self.content.format(**context)
        except KeyError as e:
            logger.warning(f"Missing context variable in prompt component {self.name}: {e}")
            return self.content


@dataclass
class ToolOrchestrationPlan:
    """Plan for orchestrating multiple tool executions"""
    task_id: str
    tools_required: List[str]
    execution_strategy: ToolExecutionStrategy
    dependencies: Dict[str, List[str]]  # tool -> [dependent_tools]
    timeout_seconds: int = 300
    retry_policy: Dict[str, Any] = field(default_factory=dict)
    context_requirements: List[str] = field(default_factory=list)

    def get_execution_order(self) -> List[List[str]]:
        """Get tools organized by execution order based on dependencies"""
        # Topological sort for dependency resolution
        visited = set()
        order = []

        def visit(tool):
            if tool in visited:
                return
            visited.add(tool)
            for dep in self.dependencies.get(tool, []):
                visit(dep)
            order.append(tool)

        for tool in self.tools_required:
            visit(tool)

        # Group tools that can run in parallel
        levels = []
        remaining = set(order)
        while remaining:
            level = []
            for tool in list(remaining):
                deps = self.dependencies.get(tool, [])
                if all(d not in remaining for d in deps):
                    level.append(tool)
            if not level:
                break
            levels.append(level)
            remaining -= set(level)

        return levels


# ============================================================
# ENHANCED CONFIGURATION MODEL
# ============================================================

class PersonaConfig(BaseModel):
    """Enhanced configuration for autonomous AI agent personas"""

    # Core Identity
    name: str = Field(..., min_length=1, max_length=50)
    persona_id: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=10, max_length=1000)
    version: str = Field(default="3.0.0", pattern=r"^\d+\.\d+\.\d+$")

    # Agent Capabilities
    capabilities: List[AgentCapability] = Field(..., min_items=1)
    specializations: List[str] = Field(default_factory=list)
    expertise_level: int = Field(default=7, ge=1, le=10)

    # LLM Configuration
    llm_preferences: Dict[str, Any] = Field(default_factory=lambda: {
        "primary_model": "gemini-1.5-pro",
        "fallback_models": ["gemini-1.5-flash", "claude-3"],
        "temperature": 0.1,
        "max_tokens": 8192,
        "top_p": 0.95,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0
    })

    # Communication Settings
    communication_style: CommunicationStyle = Field(default=CommunicationStyle.BALANCED)
    response_mode: ResponseMode = Field(default=ResponseMode.EXPLANATORY)
    verbosity_level: int = Field(default=5, ge=1, le=10)
    use_examples: bool = Field(default=True)
    use_analogies: bool = Field(default=False)

    # Tool Orchestration
    tool_execution_strategy: ToolExecutionStrategy = Field(default=ToolExecutionStrategy.ADAPTIVE)
    max_parallel_tools: int = Field(default=5, ge=1, le=20)
    tool_timeout_seconds: int = Field(default=60, ge=10, le=600)
    auto_retry_failed_tools: bool = Field(default=True)
    max_tool_retries: int = Field(default=3, ge=1, le=10)

    # Context Management
    context_window_size: int = Field(default=128000, ge=4000, le=1000000)
    context_compression_ratio: float = Field(default=0.3, ge=0.1, le=1.0)
    maintain_conversation_history: bool = Field(default=True)
    max_history_messages: int = Field(default=50, ge=10, le=1000)

    # Prompt Engineering
    system_instruction: str = Field(..., min_length=100)
    prompt_components: List[PromptComponent] = Field(default_factory=list)
    dynamic_prompting: bool = Field(default=True)
    use_chain_of_thought: bool = Field(default=True)
    use_self_reflection: bool = Field(default=True)

    # Autonomous Behavior
    proactive_suggestions: bool = Field(default=True)
    auto_error_correction: bool = Field(default=True)
    learn_from_feedback: bool = Field(default=True)
    adapt_to_user_style: bool = Field(default=True)

    # Performance & Optimization
    cache_responses: bool = Field(default=True)
    cache_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    optimize_token_usage: bool = Field(default=True)
    batch_similar_requests: bool = Field(default=True)

    # Safety & Compliance
    content_filtering: bool = Field(default=True)
    pii_detection: bool = Field(default=True)
    secure_mode: bool = Field(default=False)
    audit_logging: bool = Field(default=True)

    # Metadata
    author: Optional[str] = Field(None, max_length=100)
    tags: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    last_modified: datetime = Field(default_factory=datetime.now)

    @field_validator('capabilities')
    @classmethod
    def validate_capabilities(cls, v):
        """Ensure capabilities are valid and non-empty"""
        if not v:
            raise ValueError("Agent must have at least one capability")
        return list(set(v))  # Remove duplicates

    @model_validator(mode='before')
    @classmethod
    def validate_config_consistency(cls, values):
        """Validate configuration consistency"""
        # If secure_mode is on, ensure safety features are enabled
        if values.get('secure_mode'):
            values['content_filtering'] = True
            values['pii_detection'] = True
            values['audit_logging'] = True

        # Adjust token limits based on model
        model = values.get('llm_preferences', {}).get('primary_model')
        if model and 'gemini' in model.lower():
            max_context = 1000000 if 'pro' in model.lower() else 128000
            values['context_window_size'] = min(values.get('context_window_size', 128000), max_context)

        return values

    class Config:
        use_enum_values = True
        validate_assignment = True
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


# ============================================================
# ADVANCED METRICS TRACKING
# ============================================================

@dataclass
class AgentMetrics:
    """Comprehensive metrics for autonomous agent performance"""

    # Interaction Metrics
    total_interactions: int = 0
    successful_completions: int = 0
    partial_completions: int = 0
    failed_attempts: int = 0

    # Tool Usage Metrics
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    failed_tool_calls: int = 0
    tools_usage_count: Dict[str, int] = field(default_factory=dict)

    # Performance Metrics
    average_response_time: float = 0.0
    average_tokens_used: float = 0.0
    total_tokens_consumed: int = 0
    cache_hit_rate: float = 0.0

    # Quality Metrics
    user_satisfaction_score: float = 0.0
    task_complexity_handled: float = 0.0
    error_recovery_rate: float = 0.0

    # Learning Metrics
    adaptations_made: int = 0
    patterns_learned: int = 0
    feedback_incorporated: int = 0

    # Time-based Metrics
    uptime_seconds: float = 0.0
    last_interaction: Optional[datetime] = None
    peak_usage_time: Optional[datetime] = None

    # Historical Data
    hourly_interactions: deque = field(default_factory=lambda: deque(maxlen=24))
    daily_success_rate: deque = field(default_factory=lambda: deque(maxlen=30))

    @property
    def success_rate(self) -> float:
        """Calculate overall success rate"""
        total = self.successful_completions + self.partial_completions + self.failed_attempts
        if total == 0:
            return 0.0
        return (self.successful_completions / total) * 100

    @property
    def tool_efficiency(self) -> float:
        """Calculate tool execution efficiency"""
        if self.total_tool_calls == 0:
            return 0.0
        return (self.successful_tool_calls / self.total_tool_calls) * 100

    def record_interaction(self,
                          success_level: str,  # 'full', 'partial', 'failed'
                          response_time: float,
                          tokens_used: int,
                          tools_called: List[str],
                          user_satisfaction: Optional[float] = None,
                          complexity: Optional[float] = None):
        """Record a complete interaction with the agent"""
        self.total_interactions += 1

        # Update completion counters
        if success_level == 'full':
            self.successful_completions += 1
        elif success_level == 'partial':
            self.partial_completions += 1
        else:
            self.failed_attempts += 1

        # Update tool metrics
        for tool in tools_called:
            self.tools_usage_count[tool] = self.tools_usage_count.get(tool, 0) + 1

        # Update performance metrics
        self._update_average('average_response_time', response_time)
        self._update_average('average_tokens_used', tokens_used)
        self.total_tokens_consumed += tokens_used

        # Update quality metrics
        if user_satisfaction is not None:
            self._update_average('user_satisfaction_score', user_satisfaction)
        if complexity is not None:
            self._update_average('task_complexity_handled', complexity)

        # Update timestamps
        self.last_interaction = datetime.now()

        # Update hourly tracking
        current_hour = datetime.now().hour
        if len(self.hourly_interactions) == 0 or self.hourly_interactions[-1][0] != current_hour:
            self.hourly_interactions.append([current_hour, 1])
        else:
            self.hourly_interactions[-1][1] += 1

    def _update_average(self, field: str, new_value: float):
        """Update running average for a field"""
        current = getattr(self, field)
        count = self.total_interactions
        if count == 1:
            setattr(self, field, new_value)
        else:
            setattr(self, field, (current * (count - 1) + new_value) / count)

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get comprehensive performance summary"""
        return {
            "success_rate": f"{self.success_rate:.2f}%",
            "tool_efficiency": f"{self.tool_efficiency:.2f}%",
            "avg_response_time": f"{self.average_response_time:.3f}s",
            "avg_tokens": f"{self.average_tokens_used:.0f}",
            "total_tokens": self.total_tokens_consumed,
            "satisfaction": f"{self.user_satisfaction_score:.2f}/10" if self.user_satisfaction_score > 0 else "N/A",
            "complexity_handled": f"{self.task_complexity_handled:.2f}/10" if self.task_complexity_handled > 0 else "N/A",
            "most_used_tools": sorted(self.tools_usage_count.items(), key=lambda x: x[1], reverse=True)[:5]
        }


# ============================================================
# CONTEXT MANAGEMENT
# ============================================================

@dataclass
class AgentContext:
    """Rich context for agent decision making"""

    # Session Context
    session_id: str
    user_id: Optional[str] = None
    conversation_history: deque = field(default_factory=lambda: deque(maxlen=100))

    # Task Context
    current_task: Optional[str] = None
    task_stack: List[str] = field(default_factory=list)
    completed_tasks: Set[str] = field(default_factory=set)

    # Tool Context
    available_tools: List[str] = field(default_factory=list)
    tool_results_cache: Dict[str, Any] = field(default_factory=dict)
    pending_tool_calls: List[str] = field(default_factory=list)

    # Knowledge Context
    learned_patterns: Dict[str, Any] = field(default_factory=dict)
    user_preferences: Dict[str, Any] = field(default_factory=dict)
    domain_knowledge: Dict[str, Any] = field(default_factory=dict)

    # Environmental Context
    working_directory: Optional[str] = None
    environment_variables: Dict[str, str] = field(default_factory=dict)
    system_capabilities: Dict[str, bool] = field(default_factory=dict)

    # Performance Context
    time_budget: Optional[float] = None
    token_budget: Optional[int] = None
    quality_requirements: Dict[str, Any] = field(default_factory=dict)

    def add_conversation_turn(self, role: str, content: str, metadata: Dict[str, Any] = None):
        """Add a conversation turn to history"""
        turn = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }
        self.conversation_history.append(turn)

    def get_relevant_history(self, max_tokens: int = 4000) -> List[Dict]:
        """Get relevant conversation history within token limit"""
        relevant = []
        token_count = 0

        for turn in reversed(self.conversation_history):
            # Rough token estimation (4 chars per token)
            turn_tokens = len(turn['content']) // 4
            if token_count + turn_tokens > max_tokens:
                break
            relevant.insert(0, turn)
            token_count += turn_tokens

        return relevant

    def update_user_preference(self, key: str, value: Any):
        """Update learned user preference"""
        self.user_preferences[key] = value
        logger.debug(f"Updated user preference: {key} = {value}")

    def cache_tool_result(self, tool_call: str, result: Any, ttl_seconds: int = 300):
        """Cache tool execution result"""
        cache_key = hashlib.md5(tool_call.encode()).hexdigest()
        self.tool_results_cache[cache_key] = {
            "result": result,
            "timestamp": time.time(),
            "ttl": ttl_seconds
        }

    def get_cached_tool_result(self, tool_call: str) -> Optional[Any]:
        """Get cached tool result if still valid"""
        cache_key = hashlib.md5(tool_call.encode()).hexdigest()
        if cache_key in self.tool_results_cache:
            cached = self.tool_results_cache[cache_key]
            if time.time() - cached['timestamp'] < cached['ttl']:
                return cached['result']
            else:
                del self.tool_results_cache[cache_key]
        return None


# ============================================================
# BASE PERSONA CLASSES
# ============================================================

class BasePersona(ABC):
    """Base abstract class for all personas in DEILE system"""

    def __init__(self, config: PersonaConfig):
        """Initialize base persona with configuration"""
        self.config = config
        self.name = config.name
        self.persona_id = config.persona_id

    @abstractmethod
    def get_persona_instructions(self) -> str:
        """Get persona-specific instructions"""
        pass

    @abstractmethod
    def get_capabilities(self) -> List[str]:
        """Get list of persona capabilities"""
        pass


class BaseAutonomousPersona(BasePersona):
    """
    Base class for autonomous AI agent personas.
    Implements core functionality for LLM-based agents with tool orchestration.
    """

    def __init__(self, config: PersonaConfig):
        super().__init__(config)
        self.metrics = AgentMetrics()
        self.context = None  # Set when activated with session
        self._is_active = False
        self._start_time = time.time()

        # Caching
        self._response_cache: Dict[str, Tuple[str, float]] = {}
        self._prompt_cache: Dict[str, str] = {}

        # Tool orchestration
        self._tool_plans: Dict[str, ToolOrchestrationPlan] = {}
        self._execution_history: deque = deque(maxlen=100)

        logger.info(f"Autonomous Persona '{self.config.name}' initialized")
        logger.info(f"Capabilities: {[c.value for c in self.config.capabilities]}")
        logger.info(f"Model: {self.config.llm_preferences['primary_model']}")

    # ========== PROPERTIES ==========

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def persona_id(self) -> str:
        return self.config.persona_id

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def uptime(self) -> float:
        return time.time() - self._start_time

    # ========== ABSTRACT METHOD IMPLEMENTATIONS ==========

    def get_persona_instructions(self) -> str:
        """Get persona-specific instructions"""
        return f"""You are {self.config.name}, an autonomous AI agent.
Communication Style: {self.config.communication_style.value}
Capabilities: {[cap.value for cap in self.config.capabilities]}
"""

    def get_capabilities(self) -> List[str]:
        """Get list of persona capabilities"""
        return [cap.value for cap in self.config.capabilities]

    # ========== ACTIVATION & LIFECYCLE ==========

    def activate(self, session_id: str, context: Optional[AgentContext] = None) -> None:
        """Activate persona with session context"""
        self._is_active = True
        self.context = context or AgentContext(session_id=session_id)
        self.metrics.uptime_seconds = self.uptime
        logger.info(f"Persona '{self.name}' activated for session {session_id}")

    def deactivate(self) -> None:
        """Deactivate persona and cleanup"""
        self._is_active = False
        self._response_cache.clear()
        self._prompt_cache.clear()
        logger.info(f"Persona '{self.name}' deactivated after {self.uptime:.2f}s")

    # ========== ABSTRACT METHODS ==========

    @abstractmethod
    async def build_system_instruction(self, context: AgentContext) -> str:
        """
        Build dynamic system instruction based on context.
        Must be implemented by concrete personas.
        """
        pass

    @abstractmethod
    async def process_user_input(self,
                                user_input: str,
                                context: AgentContext) -> Dict[str, Any]:
        """
        Process user input and return structured response.
        Must be implemented by concrete personas.
        """
        pass

    @abstractmethod
    async def select_tools(self,
                          task: str,
                          context: AgentContext) -> List[str]:
        """
        Select appropriate tools for the task.
        Must be implemented by concrete personas.
        """
        pass

    # ========== PROMPT ENGINEERING ==========

    async def build_dynamic_prompt(self,
                                  base_instruction: str,
                                  context: AgentContext,
                                  task: str) -> str:
        """Build dynamic prompt with all components"""
        components = []

        # Add base instruction
        components.append(base_instruction)

        # Add dynamic components based on priority
        if self.config.prompt_components:
            sorted_components = sorted(
                self.config.prompt_components,
                key=lambda x: x.priority,
                reverse=True
            )

            for component in sorted_components:
                if component.should_include(context.__dict__):
                    components.append(component.render(context.__dict__))

        # Add chain of thought if enabled
        if self.config.use_chain_of_thought:
            components.append(self._build_chain_of_thought_prompt(task))

        # Add self-reflection if enabled
        if self.config.use_self_reflection:
            components.append(self._build_self_reflection_prompt())

        # Add relevant context
        components.append(self._build_context_prompt(context))

        # Add task
        components.append(f"\nCurrent Task: {task}")

        return "\n\n".join(components)

    def _build_chain_of_thought_prompt(self, task: str) -> str:
        """Build chain of thought reasoning prompt"""
        return f"""
Use step-by-step reasoning to approach this task:
1. Understand the requirements
2. Break down the problem
3. Identify necessary tools and resources
4. Plan the execution strategy
5. Consider edge cases and error handling
6. Provide the solution

Task: {task}
"""

    def _build_self_reflection_prompt(self) -> str:
        """Build self-reflection prompt"""
        return """
After completing the task:
- Verify the solution meets all requirements
- Check for potential improvements
- Consider alternative approaches
- Identify any limitations or caveats
"""

    def _build_context_prompt(self, context: AgentContext) -> str:
        """Build context-aware prompt section"""
        prompt_parts = []

        # Add recent conversation context
        if context.conversation_history:
            recent = context.get_relevant_history(max_tokens=2000)
            if recent:
                prompt_parts.append("Recent conversation context:")
                for turn in recent[-3:]:  # Last 3 turns
                    prompt_parts.append(f"{turn['role']}: {turn['content'][:200]}...")

        # Add task stack context
        if context.task_stack:
            prompt_parts.append(f"Current task stack: {' -> '.join(context.task_stack[-3:])}")

        # Add available tools
        if context.available_tools:
            prompt_parts.append(f"Available tools: {', '.join(context.available_tools[:10])}")

        return "\n".join(prompt_parts) if prompt_parts else ""

    # ========== TOOL ORCHESTRATION ==========

    async def create_tool_orchestration_plan(self,
                                            task: str,
                                            required_tools: List[str],
                                            context: AgentContext) -> ToolOrchestrationPlan:
        """Create an orchestration plan for tool execution"""
        # Analyze tool dependencies
        dependencies = self._analyze_tool_dependencies(required_tools)

        # Determine execution strategy
        strategy = self._determine_execution_strategy(required_tools, dependencies)

        # Create plan
        plan = ToolOrchestrationPlan(
            task_id=hashlib.md5(task.encode()).hexdigest()[:8],
            tools_required=required_tools,
            execution_strategy=strategy,
            dependencies=dependencies,
            timeout_seconds=self.config.tool_timeout_seconds,
            retry_policy={
                "max_retries": self.config.max_tool_retries,
                "backoff_factor": 2.0,
                "retry_on": ["timeout", "rate_limit", "temporary_failure"]
            },
            context_requirements=self._identify_context_requirements(required_tools)
        )

        self._tool_plans[plan.task_id] = plan
        return plan

    def _analyze_tool_dependencies(self, tools: List[str]) -> Dict[str, List[str]]:
        """Analyze dependencies between tools"""
        dependencies = {}

        # Define known dependencies
        known_deps = {
            "test_runner": ["code_analyzer"],
            "deploy": ["test_runner", "build"],
            "performance_profiler": ["test_runner"],
            "security_scanner": ["code_analyzer"],
        }

        for tool in tools:
            if tool in known_deps:
                dependencies[tool] = [dep for dep in known_deps[tool] if dep in tools]

        return dependencies

    def _determine_execution_strategy(self,
                                     tools: List[str],
                                     dependencies: Dict[str, List[str]]) -> ToolExecutionStrategy:
        """Determine optimal execution strategy"""
        if not dependencies:
            # No dependencies, can run in parallel
            if len(tools) > self.config.max_parallel_tools:
                return ToolExecutionStrategy.BATCH
            return ToolExecutionStrategy.PARALLEL

        # Has dependencies
        if len(tools) > 10:
            return ToolExecutionStrategy.ORCHESTRATED
        elif self._is_pipeline_pattern(tools, dependencies):
            return ToolExecutionStrategy.PIPELINE
        else:
            return ToolExecutionStrategy.ADAPTIVE

    def _is_pipeline_pattern(self, tools: List[str], dependencies: Dict[str, List[str]]) -> bool:
        """Check if tools form a pipeline pattern"""
        # Simple check: each tool depends on at most one other
        for deps in dependencies.values():
            if len(deps) > 1:
                return False
        return True

    def _identify_context_requirements(self, tools: List[str]) -> List[str]:
        """Identify what context is needed for tools"""
        requirements = set()

        context_map = {
            "code_analyzer": ["source_files", "language"],
            "test_runner": ["test_files", "test_framework"],
            "deploy": ["deployment_config", "environment"],
            "database_query": ["connection_string", "schema"],
        }

        for tool in tools:
            if tool in context_map:
                requirements.update(context_map[tool])

        return list(requirements)

    # ========== CACHING & OPTIMIZATION ==========

    def _get_cache_key(self, task: str, context_hash: str) -> str:
        """Generate cache key for response"""
        return hashlib.md5(f"{task}:{context_hash}".encode()).hexdigest()

    def _should_use_cache(self, cache_key: str) -> bool:
        """Check if cached response should be used"""
        if not self.config.cache_responses:
            return False

        if cache_key in self._response_cache:
            _, timestamp = self._response_cache[cache_key]
            age = time.time() - timestamp
            return age < self.config.cache_ttl_seconds

        return False

    def _cache_response(self, cache_key: str, response: str):
        """Cache a response"""
        if self.config.cache_responses:
            self._response_cache[cache_key] = (response, time.time())

            # Cleanup old cache entries
            if len(self._response_cache) > 1000:
                self._cleanup_cache()

    def _cleanup_cache(self):
        """Remove expired cache entries"""
        current_time = time.time()
        expired_keys = [
            key for key, (_, timestamp) in self._response_cache.items()
            if current_time - timestamp > self.config.cache_ttl_seconds
        ]

        for key in expired_keys:
            del self._response_cache[key]

    # ========== ADAPTIVE BEHAVIOR ==========

    async def adapt_to_feedback(self, feedback: Dict[str, Any]):
        """Adapt behavior based on user feedback"""
        if not self.config.learn_from_feedback:
            return

        feedback_type = feedback.get('type', 'general')

        if feedback_type == 'style':
            # Adapt communication style
            self._adapt_communication_style(feedback)
        elif feedback_type == 'performance':
            # Adapt performance parameters
            self._adapt_performance_params(feedback)
        elif feedback_type == 'tool_preference':
            # Update tool preferences
            self._update_tool_preferences(feedback)

        self.metrics.feedback_incorporated += 1
        logger.info(f"Adapted to {feedback_type} feedback")

    def _adapt_communication_style(self, feedback: Dict[str, Any]):
        """Adapt communication style based on feedback"""
        if 'verbosity' in feedback:
            self.config.verbosity_level = max(1, min(10, feedback['verbosity']))

        if 'style' in feedback and feedback['style'] in CommunicationStyle.__members__:
            self.config.communication_style = CommunicationStyle[feedback['style']]

    def _adapt_performance_params(self, feedback: Dict[str, Any]):
        """Adapt performance parameters"""
        if 'speed_vs_quality' in feedback:
            # Adjust temperature and timeout based on preference
            preference = feedback['speed_vs_quality']  # -1 (speed) to 1 (quality)
            self.config.llm_preferences['temperature'] = 0.1 + (preference * 0.4)
            self.config.tool_timeout_seconds = 30 + int(preference * 30)

    def _update_tool_preferences(self, feedback: Dict[str, Any]):
        """Update tool usage preferences"""
        if 'preferred_tools' in feedback and self.context:
            self.context.user_preferences['preferred_tools'] = feedback['preferred_tools']

        if 'avoided_tools' in feedback and self.context:
            self.context.user_preferences['avoided_tools'] = feedback['avoided_tools']

    # ========== MONITORING & DIAGNOSTICS ==========

    async def get_diagnostics(self) -> Dict[str, Any]:
        """Get comprehensive diagnostics"""
        return {
            "persona": {
                "name": self.name,
                "id": self.persona_id,
                "active": self.is_active,
                "uptime": self.uptime,
                "version": self.config.version
            },
            "configuration": {
                "model": self.config.llm_preferences['primary_model'],
                "capabilities": len(self.config.capabilities),
                "communication_style": self.config.communication_style,
                "execution_strategy": self.config.tool_execution_strategy
            },
            "metrics": self.metrics.get_performance_summary(),
            "cache": {
                "response_cache_size": len(self._response_cache),
                "prompt_cache_size": len(self._prompt_cache),
                "tool_plans_cached": len(self._tool_plans)
            },
            "context": {
                "has_context": self.context is not None,
                "conversation_length": len(self.context.conversation_history) if self.context else 0,
                "task_stack_depth": len(self.context.task_stack) if self.context else 0
            } if self.context else {}
        }

    def __repr__(self) -> str:
        return f"<AutonomousPersona: {self.name} [{self.persona_id}] - {self.config.llm_preferences['primary_model']}>"