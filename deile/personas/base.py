"""
DEILE Autonomous AI Agent Persona System - ULTRA RIGOROSO
============================================================
Base classes for autonomous AI agents using LLM APIs with function calling.
Implements best practices for production-grade AI agents.

Author: DEILE Team
Version: 3.0.0 ULTRA
License: MIT
"""

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from ._persona_models import (  # noqa: F401  (re-export — superfície pública estável)
    AgentCapability,
    AgentContext,
    AgentMetrics,
    CommunicationStyle,
    PersonaConfig,
    PromptComponent,
    ResponseMode,
    ToolExecutionStrategy,
    ToolOrchestrationPlan,
)

logger = logging.getLogger(__name__)


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
            task_id=hashlib.md5(task.encode(), usedforsecurity=False).hexdigest()[:8],
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
        return hashlib.md5(f"{task}:{context_hash}".encode(), usedforsecurity=False).hexdigest()

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
