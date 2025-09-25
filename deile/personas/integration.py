"""
Persona Integration Layer - Connects personas with DeileAgent core systems
=======================================================================

Implements the architectural integration between personas and DeileAgent,
ensuring personas operate as behavioral modifiers rather than independent agents.

This integration layer uses the PersonaMemoryLayer from the unified memory system
to ensure all persona operations work through DEILE's core memory infrastructure.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

import logging
import asyncio
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from datetime import datetime

# TYPE_CHECKING imports to avoid circular dependencies
if TYPE_CHECKING:
    from ..core.agent import DeileAgent, AgentSession
    from .base import BasePersona, PersonaConfig
    from .manager import PersonaManager
    from .memory.integration import PersonaMemoryLayer

logger = logging.getLogger(__name__)


@dataclass
class PersonaIntegrationContext:
    """Context for persona integration with DeileAgent"""
    agent: 'DeileAgent'
    persona_manager: 'PersonaManager'
    current_persona: Optional['BasePersona'] = None
    session_id: str = "default"
    enhanced_context: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_active_persona(self) -> bool:
        """Check if there's an active persona"""
        return self.current_persona is not None


class PersonaEnhancedAgent:
    """
    Enhances DeileAgent with persona-aware behavior using composition pattern.

    This class acts as a wrapper around DeileAgent, adding persona functionality
    while maintaining the agent as the central orchestrator.
    """

    def __init__(self, base_agent: 'DeileAgent', persona_manager: Optional['PersonaManager'] = None):
        """
        Initialize persona-enhanced agent

        Args:
            base_agent: The core DeileAgent instance
            persona_manager: Optional PersonaManager, will be created if not provided
        """
        self.base_agent = base_agent
        self.persona_manager = persona_manager
        self.integration_context: Optional[PersonaIntegrationContext] = None

        # If no persona manager provided, create one with agent's memory manager
        if not self.persona_manager:
            from .manager import PersonaManager
            memory_manager = getattr(base_agent, 'memory_manager', None)
            self.persona_manager = PersonaManager(memory_manager=memory_manager)

            if memory_manager:
                logger.info("PersonaEnhancedAgent created with memory integration")
            else:
                logger.warning("PersonaEnhancedAgent created without memory integration")

        # Set up integration
        self._setup_agent_integration()

        logger.info("PersonaEnhancedAgent initialized with persona integration")

    async def initialize(self) -> None:
        """Initialize the enhanced agent and persona systems"""
        # Initialize base agent
        await self.base_agent.initialize()

        # Initialize persona manager if not already done
        if self.persona_manager and not hasattr(self.persona_manager, '_initialized'):
            await self.persona_manager.initialize()
            self.persona_manager._initialized = True

        # Create integration context
        self.integration_context = PersonaIntegrationContext(
            agent=self.base_agent,
            persona_manager=self.persona_manager
        )

        # Set up deep integration points
        await self._setup_deep_integration()

        logger.info("PersonaEnhancedAgent fully initialized with deep integration")

    async def process_input_with_persona(
        self,
        user_input: str,
        session_id: str = "default",
        **kwargs
    ) -> 'AgentResponse':
        """
        Process input with persona-enhanced behavior

        This is the main entry point that demonstrates persona enhancement
        while keeping DeileAgent as the central orchestrator.
        """
        try:
            # Update integration context
            if self.integration_context:
                self.integration_context.session_id = session_id
                self.integration_context.current_persona = await self._get_current_persona()

            # If no active persona, use standard agent processing
            if not self._has_active_persona():
                return await self.base_agent.process_input(user_input, session_id, **kwargs)

            # Enhance context with persona information
            enhanced_kwargs = await self._enhance_context_with_persona(user_input, session_id, **kwargs)

            # Use base agent's processing with persona enhancements
            response = await self.base_agent.process_input(
                user_input=user_input,
                session_id=session_id,
                **enhanced_kwargs
            )

            # Post-process response with persona insights
            if self.integration_context and self.integration_context.current_persona:
                response = await self._post_process_with_persona(response, user_input, session_id)

            return response

        except Exception as e:
            logger.error(f"Error in persona-enhanced processing: {e}")
            # Fallback to standard agent processing
            return await self.base_agent.process_input(user_input, session_id, **kwargs)

    async def switch_persona(self, persona_id: str, session_id: str = "default") -> bool:
        """Switch to a different persona"""
        if not self.persona_manager:
            return False

        try:
            success = await self.persona_manager.switch_persona(persona_id, session_id)

            if success and self.integration_context:
                self.integration_context.current_persona = await self._get_current_persona()
                self.integration_context.session_id = session_id

                # Update agent systems with new persona context
                await self._update_agent_systems_with_persona()

            return success

        except Exception as e:
            logger.error(f"Error switching persona to {persona_id}: {e}")
            return False

    def _setup_agent_integration(self) -> None:
        """Set up basic integration points with the agent"""
        # Set persona manager in base agent if not already set
        if not self.base_agent.persona_manager:
            self.base_agent.persona_manager = self.persona_manager

    async def _setup_deep_integration(self) -> None:
        """Set up deep integration with agent systems"""
        try:
            # Integrate with context manager
            if hasattr(self.base_agent.context_manager, 'set_persona_integration'):
                self.base_agent.context_manager.set_persona_integration(self.persona_manager)

            # Integrate with tool registry (if method exists)
            if hasattr(self.base_agent.tool_registry, 'register_persona_tools'):
                self.base_agent.tool_registry.register_persona_tools(self.persona_manager)

            # Set up intent analyzer integration
            if hasattr(self.base_agent, 'intent_analyzer') and hasattr(self.base_agent.intent_analyzer, 'set_persona_context'):
                self.base_agent.intent_analyzer.set_persona_context(self.persona_manager)

            logger.debug("Deep integration setup completed")

        except Exception as e:
            logger.warning(f"Some deep integration features unavailable: {e}")

    async def _get_current_persona(self) -> Optional['BasePersona']:
        """Get the currently active persona"""
        if not self.persona_manager:
            return None

        try:
            return self.persona_manager.get_current_persona()
        except Exception as e:
            logger.debug(f"No current persona available: {e}")
            return None

    def _has_active_persona(self) -> bool:
        """Check if there's an active persona"""
        return (self.integration_context and
                self.integration_context.has_active_persona)

    async def _enhance_context_with_persona(
        self,
        user_input: str,
        session_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Enhance processing context with persona information"""
        enhanced_kwargs = kwargs.copy()

        if not self._has_active_persona():
            return enhanced_kwargs

        try:
            current_persona = self.integration_context.current_persona

            # Add persona context to kwargs
            persona_context = {
                'persona_id': current_persona.persona_id if current_persona else None,
                'persona_name': current_persona.name if current_persona else None,
                'persona_capabilities': (
                    [cap.value for cap in current_persona.config.capabilities]
                    if current_persona else []
                ),
                'communication_style': (
                    current_persona.config.communication_style.value
                    if current_persona else None
                ),
                'response_mode': (
                    current_persona.config.response_mode.value
                    if current_persona else None
                )
            }

            enhanced_kwargs['persona_context'] = persona_context

            # Store enhanced context for other integration points
            if self.integration_context:
                self.integration_context.enhanced_context = persona_context

            logger.debug(f"Enhanced context with persona {persona_context['persona_id']}")

        except Exception as e:
            logger.warning(f"Failed to enhance context with persona: {e}")

        return enhanced_kwargs

    async def _post_process_with_persona(
        self,
        response: 'AgentResponse',
        user_input: str,
        session_id: str
    ) -> 'AgentResponse':
        """Post-process response with persona insights"""
        if not self._has_active_persona():
            return response

        try:
            current_persona = self.integration_context.current_persona

            # Add persona metadata to response
            if not response.metadata:
                response.metadata = {}

            response.metadata.update({
                'persona_enhanced': True,
                'persona_id': current_persona.persona_id,
                'persona_name': current_persona.name,
                'integration_version': '5.0.0'
            })

            # Store interaction in persona memory if available
            if hasattr(self.persona_manager, 'store_interaction'):
                await self.persona_manager.store_interaction(
                    user_input, response.content, session_id
                )

            logger.debug(f"Post-processed response with persona {current_persona.persona_id}")

        except Exception as e:
            logger.warning(f"Failed to post-process with persona: {e}")

        return response

    async def _update_agent_systems_with_persona(self) -> None:
        """Update core agent systems with current persona context"""
        if not self._has_active_persona():
            return

        try:
            current_persona = self.integration_context.current_persona

            # Update context manager with persona instructions
            if hasattr(self.base_agent.context_manager, 'set_persona_instructions'):
                system_instructions = await current_persona.build_system_instruction(
                    self.integration_context
                ) if current_persona else None

                if system_instructions:
                    await self.base_agent.context_manager.set_persona_instructions(
                        system_instructions
                    )

            # Update intent analyzer with persona patterns
            if (hasattr(self.base_agent, 'intent_analyzer') and
                hasattr(self.base_agent.intent_analyzer, 'add_persona_patterns')):

                if hasattr(current_persona, 'get_intent_patterns'):
                    patterns = current_persona.get_intent_patterns()
                    await self.base_agent.intent_analyzer.add_persona_patterns(patterns)

            logger.debug(f"Updated agent systems with persona {current_persona.persona_id}")

        except Exception as e:
            logger.warning(f"Failed to update agent systems with persona: {e}")

    # Delegation methods - forward agent functionality while maintaining integration

    async def get_stats(self) -> Dict[str, Any]:
        """Get enhanced stats including persona information"""
        base_stats = await self.base_agent.get_stats()

        persona_stats = {}
        if self.persona_manager:
            try:
                persona_stats = {
                    'active_persona': (
                        self.integration_context.current_persona.persona_id
                        if self._has_active_persona() else None
                    ),
                    'available_personas': len(self.persona_manager._personas),
                    'total_switches': getattr(self.persona_manager, '_total_switches', 0)
                }
            except Exception as e:
                logger.debug(f"Failed to get persona stats: {e}")

        base_stats['persona_integration'] = persona_stats
        return base_stats

    def get_session(self, session_id: str):
        """Forward session management to base agent"""
        return self.base_agent.get_session(session_id)

    def create_session(self, session_id: str, **kwargs):
        """Forward session creation to base agent"""
        return self.base_agent.create_session(session_id, **kwargs)

    async def get_available_tools(self):
        """Forward tool listing to base agent"""
        return await self.base_agent.get_available_tools()

    async def get_available_parsers(self):
        """Forward parser listing to base agent"""
        return await self.base_agent.get_available_parsers()

    @property
    def status(self):
        """Forward status to base agent"""
        return self.base_agent.status

    @property
    def request_count(self):
        """Forward request count to base agent"""
        return self.base_agent.request_count

    def __repr__(self) -> str:
        persona_info = ""
        if self._has_active_persona():
            persona_info = f" + Persona({self.integration_context.current_persona.persona_id})"
        return f"<PersonaEnhancedAgent: {self.base_agent}{persona_info}>"


class PersonaIntegrationLayer:
    """
    Coordinates persona functionality with core agent systems

    This class provides integration points for personas to work with
    DeileAgent's tools, context, and memory systems.
    """

    def __init__(self, agent: 'DeileAgent'):
        self.agent = agent
        self.persona_manager: Optional['PersonaManager'] = None

    def set_persona_manager(self, persona_manager: 'PersonaManager') -> None:
        """Set the persona manager for this integration layer"""
        self.persona_manager = persona_manager

    async def enhance_context_building(self, base_context: Dict) -> Dict:
        """Enhance context with persona-specific information"""
        if not self.persona_manager or not self.persona_manager.has_active_persona():
            return base_context

        try:
            current_persona = self.persona_manager.get_current_persona()
            if not current_persona:
                return base_context

            enhanced_context = base_context.copy()
            enhanced_context.update({
                'persona_id': current_persona.persona_id,
                'personality_traits': getattr(current_persona, 'traits', []),
                'communication_style': current_persona.config.communication_style.value,
                'specialized_capabilities': [cap.value for cap in current_persona.config.capabilities],
                'response_mode': current_persona.config.response_mode.value,
                'expertise_level': current_persona.config.expertise_level
            })

            logger.debug(f"Enhanced context with persona {current_persona.persona_id}")
            return enhanced_context

        except Exception as e:
            logger.warning(f"Failed to enhance context with persona: {e}")
            return base_context

    async def enhance_tool_selection(self, tools: List[str]) -> List[str]:
        """Filter and prioritize tools based on persona preferences"""
        if not self.persona_manager or not self.persona_manager.has_active_persona():
            return tools

        try:
            current_persona = self.persona_manager.get_current_persona()
            if not current_persona or not hasattr(current_persona, 'prioritize_tools'):
                return tools

            prioritized_tools = current_persona.prioritize_tools(tools)
            logger.debug(f"Persona {current_persona.persona_id} prioritized {len(tools)} tools")
            return prioritized_tools

        except Exception as e:
            logger.warning(f"Failed to enhance tool selection with persona: {e}")
            return tools

    async def get_persona_system_instruction(self) -> Optional[str]:
        """Get system instruction from current persona"""
        if not self.persona_manager or not self.persona_manager.has_active_persona():
            return None

        try:
            current_persona = self.persona_manager.get_current_persona()
            if not current_persona:
                return None

            # Create minimal context for instruction building
            from .base import AgentContext
            context = AgentContext(session_id="default")

            instruction = await current_persona.build_system_instruction(context)
            logger.debug(f"Generated system instruction from persona {current_persona.persona_id}")
            return instruction

        except Exception as e:
            logger.warning(f"Failed to get persona system instruction: {e}")
            return None