"""Gerenciador de personas com hot-reload e ciclo de vida completo - UNIFIED CONFIG"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from datetime import datetime

from .base import BaseAutonomousPersona, PersonaCapability
from .config import PersonaConfig  # ← Use unified configuration
from .loader import PersonaLoader
from .context import PersonaContext
from .memory.integration import PersonaMemoryLayer
from .error_context import ErrorContext, ErrorSeverity
from .error_recovery import ErrorRecoveryManager
from .audit_integration import get_persona_audit_logger
from ..core.exceptions import (
    PersonaError, PersonaLoadError, PersonaSwitchError,
    PersonaConfigError, PersonaExecutionError, PersonaInitializationError,
    PersonaIntegrationError
)

logger = logging.getLogger(__name__)


class PersonaManager:
    """Gerenciador central de personas com capacidades enterprise-grade

    Features:
    - Hot-reload de configurações
    - Ciclo de vida completo das personas
    - Validação e verificação de integridade
    - Métricas e monitoramento
    - Auto-discovery de personas
    - Integração com sistema de memória unificado DEILE
    """

    def __init__(self, agent: 'DeileAgent' = None, memory_manager=None):
        # UNIFIED CONFIGURATION: Use agent's ConfigManager
        if agent:
            self.agent = agent
            self.config_manager = agent.config_manager
            self.memory_manager = agent.memory_manager or memory_manager
            self.tool_registry = getattr(agent, 'tool_registry', None)
        else:
            # Fallback for standalone usage
            from ..config.manager import get_config_manager
            self.agent = None
            self.config_manager = get_config_manager()
            self.memory_manager = memory_manager
            self.tool_registry = None

        # Memory integration - unified memory system integration
        self._memory_integrated = self.memory_manager is not None

        if self._memory_integrated:
            logger.info(f"PersonaManager initialized with unified memory system integration")

        # Storage de personas ativas
        self._personas: Dict[str, BasePersona] = {}
        self._active_persona: Optional[str] = None
        self._current_context: Optional[PersonaContext] = None

        # UNIFIED CONFIG: Register as observer for persona configuration changes
        self.config_manager.add_persona_observer(self._on_persona_config_change)

        # Loader para carregar personas dinamicamente (still needed for instruction files)
        self.loader = PersonaLoader(self.config_manager)

        # Error handling components - unified error handling system
        self.error_recovery_manager = ErrorRecoveryManager()
        self.persona_audit_logger = get_persona_audit_logger()

        # Métricas do manager
        self._total_switches = 0
        self._last_reload_time = 0.0

        logger.info(f"PersonaManager initialized with unified configuration and error handling systems")

    async def initialize(self, enable_hot_reload: bool = True) -> None:
        """Initialize persona manager with unified configuration"""
        logger.info("Initializing PersonaManager with unified configuration...")

        try:
            # Load persona configuration from unified ConfigManager
            personas_config = await self.config_manager.load_persona_configuration()
            if not personas_config or not personas_config.get('enabled', False):
                logger.info("Personas disabled in unified configuration")
                return

            # Load available personas from unified configuration
            await self._load_available_personas()

            # Set default persona from unified configuration
            default_persona_id = personas_config.get('default_persona', 'developer')
            if default_persona_id in self._personas:
                await self.switch_persona(default_persona_id)

            # Configure unified hot-reload if requested
            if enable_hot_reload:
                await self.config_manager.setup_hot_reload()

            logger.info(f"PersonaManager initialized with {len(self._personas)} personas")

        except Exception as e:
            logger.error(f"Error initializing PersonaManager: {e}")

            # Create error context
            context = ErrorContext(
                operation="initialize_persona_manager",
                severity=ErrorSeverity.CRITICAL,
                error_type="PersonaInitializationError"
            )

            # Create proper PersonaInitializationError
            error = PersonaInitializationError(
                f"PersonaManager initialization failed: {e}",
                persona_id="system",
                initialization_step="manager_init"
            )

            # Log to audit system
            await self.persona_audit_logger.log_persona_error(error, context)

            raise error

    async def _load_available_personas(self) -> None:
        """Load all available personas from unified configuration"""
        persona_configs = await self.config_manager._get_config_value('personas.persona_configs', {})

        for persona_id, config_data in persona_configs.items():
            try:
                # Create PersonaConfig using unified configuration
                persona_config = PersonaConfig.from_dict(
                    persona_id, config_data, self.config_manager
                )

                # Create persona instance
                persona = await self._create_persona_from_config(persona_id, persona_config)
                self._personas[persona_id] = persona

                logger.debug(f"Loaded persona from unified config: {persona_id}")

            except Exception as e:
                logger.error(f"Failed to load persona {persona_id}: {e}")

                # Create error context for failed persona load
                context = ErrorContext(
                    operation="load_persona",
                    persona_id=persona_id,
                    severity=ErrorSeverity.HIGH,
                    error_type="PersonaLoadError"
                )
                context.capture_stack_trace()

                # Create proper PersonaLoadError
                load_error = PersonaLoadError(
                    f"Failed to load persona {persona_id}: {str(e)}",
                    persona_id=persona_id,
                    recovery_suggestion="Check persona configuration and dependencies"
                )

                # Log to audit system (don't raise to continue loading other personas)
                try:
                    await self.persona_audit_logger.log_persona_error(load_error, context)
                except Exception as audit_error:
                    logger.warning(f"Failed to log persona load error to audit: {audit_error}")

    async def _create_persona_from_config(
        self,
        persona_id: str,
        persona_config: PersonaConfig
    ) -> BasePersona:
        """Create persona instance with unified configuration"""
        # Create memory layer for this persona
        memory_layer = PersonaMemoryLayer(self.memory_manager, persona_id)

        # Load persona instructions
        instructions = await self.loader.load_persona_instructions(persona_id)

        # Create persona with unified configuration
        # Note: BaseAutonomousPersona expects the Pydantic PersonaConfig from base.py
        # We need to convert our unified PersonaConfig to the expected format
        try:
            # Convert unified config to base.py PersonaConfig format
            from .base import PersonaConfig as PydanticPersonaConfig

            pydantic_config = PydanticPersonaConfig(
                name=persona_config.persona_id.title(),
                persona_id=persona_config.persona_id,
                description=f"AI assistant specialized in {', '.join(persona_config.capabilities)}",
                capabilities=[],  # TODO: Map to AgentCapability enum
                model_preferences=persona_config.model_preferences.to_dict(),
                communication_style=persona_config.communication_style.value,
                system_instruction=instructions
            )

            persona = BaseAutonomousPersona(config=pydantic_config)

        except Exception as e:
            logger.warning(f"Failed to create BaseAutonomousPersona for {persona_id}: {e}")
            # Fallback: Create a minimal persona wrapper
            persona = self._create_minimal_persona(persona_id, persona_config, instructions)

        await persona.initialize()
        return persona

    def _create_minimal_persona(self, persona_id: str, config: PersonaConfig, instructions: str):
        """Create a minimal persona wrapper as fallback"""

        class MinimalPersona:
            """Minimal persona implementation for unified configuration compatibility"""

            def __init__(self, persona_id: str, config: PersonaConfig, instructions: str):
                self.id = persona_id
                self.persona_id = persona_id
                self.name = persona_id.title()
                self.config = config
                self.instructions = instructions
                self._is_active = False

            async def initialize(self):
                """Initialize the persona"""
                pass

            def activate(self, session_id: str = None, context=None):
                """Activate the persona"""
                self._is_active = True

            def deactivate(self):
                """Deactivate the persona"""
                self._is_active = False

            async def update_configuration(self, new_config: PersonaConfig):
                """Update persona configuration"""
                self.config = new_config

            async def reload_configuration(self):
                """Reload configuration"""
                pass

            def __str__(self):
                return f"MinimalPersona({self.persona_id})"

        return MinimalPersona(persona_id, config, instructions)

    # CONFIGURATION CHANGE HANDLING

    async def _on_persona_config_change(
        self,
        persona_id: str,
        new_config: Dict[str, Any],
        event_type: str
    ) -> None:
        """Handle persona configuration changes from unified ConfigManager"""
        try:
            if event_type == 'added':
                await self._handle_persona_added(persona_id, new_config)
            elif event_type == 'updated':
                await self._handle_persona_updated(persona_id, new_config)
            elif event_type == 'removed':
                await self._handle_persona_removed(persona_id)

            logger.info(f"Handled persona config change: {event_type} for {persona_id}")

        except Exception as e:
            logger.error(f"Failed to handle persona config change: {e}")

    async def _handle_persona_added(self, persona_id: str, config_data: Dict[str, Any]) -> None:
        """Handle new persona addition"""
        if persona_id not in self._personas:
            persona_config = PersonaConfig.from_dict(
                persona_id, config_data, self.config_manager
            )
            persona = await self._create_persona_from_config(persona_id, persona_config)
            self._personas[persona_id] = persona

    async def _handle_persona_updated(self, persona_id: str, config_data: Dict[str, Any]) -> None:
        """Handle persona configuration update"""
        if persona_id in self._personas:
            # Update existing persona configuration
            persona = self._personas[persona_id]
            updated_config = PersonaConfig.from_dict(
                persona_id, config_data, self.config_manager
            )

            # Update persona with new configuration
            await persona.update_configuration(updated_config)

            # If this is the current persona, update context
            if self._current_context and self._active_persona == persona_id:
                await persona.reload_configuration()

    async def _handle_persona_removed(self, persona_id: str) -> None:
        """Handle persona removal"""
        if persona_id in self._personas:
            # Clean up persona memory
            await self.cleanup_persona_memory(persona_id)

            # Remove from personas dict
            del self._personas[persona_id]

            # If this was the current persona, switch to default
            if self._current_context and self._active_persona == persona_id:
                default_persona = await self.config_manager._get_config_value(
                    'personas.default_persona', 'developer'
                )
                if default_persona in self._personas:
                    await self.switch_persona(default_persona)
                else:
                    self._active_persona = None
                    self._current_context = None

    def get_persona(self, persona_id: str) -> Optional[BasePersona]:
        """Obtém uma persona pelo ID"""
        return self._personas.get(persona_id)

    def get_active_persona(self) -> Optional[BasePersona]:
        """Obtém a persona atualmente ativa"""
        if self._active_persona:
            return self._personas.get(self._active_persona)
        return None

    async def switch_persona(
        self,
        persona_id: str,
        session_id: str = "default",
        user_context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Switch to a specific persona with comprehensive error handling

        Args:
            persona_id: ID of the persona to activate
            session_id: Current session ID
            user_context: Optional user context information

        Returns:
            bool: True if switch was successful
        """
        current_persona_name = self._active_persona or "none"

        # Create error context
        context = ErrorContext(
            operation="switch_persona",
            persona_id=persona_id,
            session_id=session_id,
            user_id=user_context.get("user_id") if user_context else None,
            operation_parameters={
                "from_persona": current_persona_name,
                "to_persona": persona_id,
                "session_id": session_id
            }
        )

        try:
            # Validate target persona exists
            if persona_id not in self._personas:
                error = PersonaSwitchError(
                    f"Target persona '{persona_id}' not found",
                    from_persona=current_persona_name,
                    to_persona=persona_id,
                    recovery_suggestion="Use /persona list to see available personas"
                )

                context.severity = ErrorSeverity.MEDIUM
                await self.persona_audit_logger.log_persona_error(error, context)

                # Log switch attempt
                await self.persona_audit_logger.log_persona_switch(
                    from_persona=current_persona_name,
                    to_persona=persona_id,
                    success=False,
                    session_id=session_id,
                    switch_reason="persona_not_found"
                )

                return False

            # Save current persona context if exists and memory manager is available
            if self._current_context and self._memory_integrated and self.memory_manager:
                await self._current_context.save_state()
                logger.debug(f"Saved current persona context for session {session_id}")

            # Deactivate current persona if exists
            if self._active_persona:
                current_persona = self._personas.get(self._active_persona)
                if current_persona:
                    current_persona.deactivate()
                    logger.debug(f"Deactivated previous persona: {self._active_persona}")

            # Create new persona context with unified memory if available
            if self._memory_integrated and self.memory_manager:
                new_context = await PersonaContext.create(
                    persona_id=persona_id,
                    session_id=session_id,
                    memory_manager=self.memory_manager
                )
                self._current_context = new_context

                # Store persona switch event in episodic memory
                await self.memory_manager.episodic_memory.record_event(
                    event_type="persona_switch",
                    session_id=session_id,
                    details={
                        'from_persona': self._active_persona,
                        'to_persona': persona_id,
                        'timestamp': datetime.now().isoformat()
                    }
                )
                logger.debug(f"Stored persona switch event in unified memory")
            else:
                logger.warning(f"Memory integration not available for persona switch")

            # Activate new persona
            new_persona = self._personas[persona_id]
            new_persona.activate(session_id=session_id)
            self._active_persona = persona_id
            self._total_switches += 1

            # Log successful switch
            await self.persona_audit_logger.log_persona_switch(
                from_persona=current_persona_name,
                to_persona=persona_id,
                success=True,
                session_id=session_id
            )

            logger.info(f"Successfully switched from '{current_persona_name}' to '{new_persona.name}' ({persona_id})")
            return True

        except PersonaError as persona_error:
            logger.error(f"Persona error during switch: {persona_error}")

            context.severity = self._assess_error_severity(persona_error)
            await self.persona_audit_logger.log_persona_error(persona_error, context)

            # Attempt recovery
            recovery_success = await self.error_recovery_manager.attempt_recovery(
                persona_error, context
            )

            if recovery_success:
                logger.info(f"Recovery successful for persona switch to {persona_id}")
                # Log successful recovery
                await self.persona_audit_logger.log_recovery_attempt(
                    persona_error, context, "fallback", True
                )
                return False  # Switch failed but recovery handled it gracefully
            else:
                # Log failed recovery
                await self.persona_audit_logger.log_recovery_attempt(
                    persona_error, context, "fallback", False
                )
                # Log failed switch
                await self.persona_audit_logger.log_persona_switch(
                    from_persona=current_persona_name,
                    to_persona=persona_id,
                    success=False,
                    session_id=session_id,
                    switch_reason="persona_error"
                )
                raise persona_error

        except Exception as unexpected_error:
            logger.error(f"Unexpected error during persona switch: {unexpected_error}")

            # Wrap in PersonaSwitchError
            switch_error = PersonaSwitchError(
                f"Unexpected error switching persona: {str(unexpected_error)}",
                from_persona=current_persona_name,
                to_persona=persona_id,
                recovery_suggestion="Check system logs and retry"
            )

            context.severity = ErrorSeverity.HIGH
            await self.persona_audit_logger.log_persona_error(switch_error, context)

            # Log failed switch
            await self.persona_audit_logger.log_persona_switch(
                from_persona=current_persona_name,
                to_persona=persona_id,
                success=False,
                session_id=session_id,
                switch_reason="unexpected_error"
            )

            raise switch_error

    def _assess_error_severity(self, error: PersonaError) -> ErrorSeverity:
        """Assess error severity based on error type and context"""
        if isinstance(error, PersonaConfigError):
            return ErrorSeverity.HIGH  # Config errors are serious
        elif isinstance(error, PersonaInitializationError):
            return ErrorSeverity.HIGH  # Init errors are serious
        elif isinstance(error, PersonaIntegrationError):
            return ErrorSeverity.HIGH  # Integration errors are serious
        elif isinstance(error, PersonaLoadError):
            return ErrorSeverity.MEDIUM  # Load errors are manageable
        elif isinstance(error, PersonaSwitchError):
            return ErrorSeverity.MEDIUM  # Switch errors are manageable
        elif isinstance(error, PersonaExecutionError):
            return ErrorSeverity.LOW  # Execution errors are often recoverable
        else:
            return ErrorSeverity.MEDIUM  # Default severity

    def list_personas(self) -> List[Dict[str, Any]]:
        """Lista todas as personas disponíveis"""
        return [
            {
                "persona_id": persona_id,
                "name": persona.name,
                "capabilities": [cap.value for cap in persona.capabilities],
                "expertise_areas": persona.expertise_areas,
                "is_active": persona.is_active,
                "communication_style": persona.config.communication_style.value,
                "expertise_level": persona.config.expertise_level
            }
            for persona_id, persona in self._personas.items()
        ]

    def list_by_capability(self, capability: PersonaCapability) -> List[str]:
        """Lista personas que possuem uma capacidade específica"""
        return [
            persona_id for persona_id, persona in self._personas.items()
            if capability in persona.capabilities
        ]

    async def find_best_persona_for_task(self, task_description: str, required_capabilities: List[PersonaCapability] = None) -> Optional[str]:
        """Encontra a melhor persona para uma tarefa específica

        Args:
            task_description: Descrição da tarefa
            required_capabilities: Capacidades obrigatórias

        Returns:
            Optional[str]: ID da melhor persona ou None se nenhuma adequada
        """
        candidates = []

        for persona_id, persona in self._personas.items():
            if persona.can_handle_task(task_description, required_capabilities):
                # Calcula score baseado em expertise e métricas
                score = persona.config.expertise_level
                if persona.metrics.success_rate > 0:
                    score += persona.metrics.success_rate / 10  # Bonus por histórico

                candidates.append((persona_id, score))

        if not candidates:
            return None

        # Retorna a persona com maior score
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    async def validate_all_personas(self) -> Dict[str, List[str]]:
        """Valida todas as personas carregadas

        Returns:
            Dict[str, List[str]]: Mapeamento persona_id -> lista de erros
        """
        validation_results = {}

        for persona_id, persona in self._personas.items():
            errors = await persona.validate_config()
            if errors:
                validation_results[persona_id] = errors

        return validation_results

    async def enhance_context(self, base_context: Dict[str, Any]) -> Dict[str, Any]:
        """Enhance base context with persona information from unified memory"""
        if not self._active_persona or not self._current_context:
            return base_context

        try:
            # Get persona-specific context from unified memory
            conversation_history = await self._current_context.memory_layer.get_conversation_history(
                self._current_context.session_id,
                limit=5
            )

            # Get learned patterns
            learned_patterns = await self._current_context.memory_layer.get_learned_patterns(
                "conversation_style"
            )

            enhanced_context = {
                **base_context,
                'persona_id': self._active_persona,
                'persona_state': self._current_context.current_state,
                'persona_preferences': self._current_context.preferences,
                'conversation_history': conversation_history,
                'learned_patterns': learned_patterns
            }

            return enhanced_context

        except Exception as e:
            logger.error(f"Error enhancing context: {e}")
            return base_context

    async def process_interaction_feedback(
        self,
        interaction_data: Dict[str, Any],
        success_metrics: Dict[str, float]
    ) -> None:
        """Process interaction feedback for learning"""
        if not self._current_context:
            return

        try:
            # Update context with interaction
            await self._current_context.update_from_interaction(interaction_data)

            # Learn from successful patterns
            if success_metrics.get('success_rate', 0) > 0.7:
                await self._current_context.memory_layer.learn_pattern(
                    pattern_type="successful_interaction",
                    pattern_data=interaction_data,
                    success_rate=success_metrics['success_rate']
                )

        except Exception as e:
            logger.error(f"Error processing interaction feedback: {e}")

    async def cleanup_persona_memory(self, persona_id: str) -> None:
        """Cleanup memory for specific persona"""
        if not self.memory_manager:
            logger.warning("No memory manager available for cleanup")
            return

        try:
            memory_layer = PersonaMemoryLayer(self.memory_manager, persona_id)
            await memory_layer.cleanup_persona_memory()
            logger.info(f"Cleaned up memory for persona {persona_id}")

        except Exception as e:
            logger.error(f"Error cleaning up persona memory: {e}")

    def set_memory_manager(self, memory_manager) -> None:
        """Set the memory manager for unified memory integration"""
        self.memory_manager = memory_manager
        self._memory_integrated = memory_manager is not None

        if self._memory_integrated:
            logger.info("Memory manager set for persona manager - unified memory integration enabled")
        else:
            logger.warning("Memory manager set to None - unified memory integration disabled")

    def validate_memory_integration(self) -> bool:
        """Validate that memory integration is working correctly"""
        if not self._memory_integrated or not self.memory_manager:
            logger.warning("Memory integration validation failed: no memory manager")
            return False

        try:
            # Check if memory manager has required components
            required_components = ['semantic_memory', 'episodic_memory', 'working_memory']
            for component in required_components:
                if not hasattr(self.memory_manager, component):
                    logger.error(f"Memory manager missing component: {component}")
                    return False

            logger.info("Memory integration validation successful")
            return True

        except Exception as e:
            logger.error(f"Memory integration validation failed: {e}")
            return False

    def has_active_persona(self) -> bool:
        """Check if there's an active persona"""
        return self._active_persona is not None

    def get_current_persona(self) -> Optional[BasePersona]:
        """Get the currently active persona (alias for get_active_persona)"""
        return self.get_active_persona()

    # CONFIGURATION MANAGEMENT METHODS

    async def add_persona(
        self,
        persona_id: str,
        persona_config: PersonaConfig
    ) -> None:
        """Add new persona using unified configuration"""
        try:
            # Save to unified configuration
            await self.config_manager.add_persona(persona_id, persona_config.to_dict())

            # The configuration observer will handle the actual persona creation
            logger.info(f"Added persona {persona_id} to unified configuration")

        except Exception as e:
            logger.error(f"Failed to add persona {persona_id}: {e}")
            raise

    async def remove_persona(self, persona_id: str) -> None:
        """Remove persona using unified configuration"""
        try:
            # Remove from unified configuration
            await self.config_manager.remove_persona(persona_id)

            # The configuration observer will handle the actual persona removal
            logger.info(f"Removed persona {persona_id} from unified configuration")

        except Exception as e:
            logger.error(f"Failed to remove persona {persona_id}: {e}")
            raise

    async def update_persona_configuration(
        self,
        persona_id: str,
        config_updates: Dict[str, Any]
    ) -> None:
        """Update persona configuration using unified system"""
        try:
            # Update in unified configuration
            await self.config_manager.update_persona_config(persona_id, config_updates)

            # The configuration observer will handle the actual persona update
            logger.info(f"Updated configuration for persona {persona_id}")

        except Exception as e:
            logger.error(f"Failed to update persona {persona_id} configuration: {e}")
            raise

    async def get_persona_configuration(self, persona_id: str) -> PersonaConfig:
        """Get persona configuration from unified system"""
        try:
            return await PersonaConfig.load_from_config_manager(
                persona_id, self.config_manager
            )
        except Exception as e:
            logger.error(f"Failed to get persona {persona_id} configuration: {e}")
            raise

    async def store_interaction(
        self,
        user_input: str,
        response: str,
        session_id: str,
        metadata: Dict[str, Any] = None
    ) -> None:
        """Store interaction in persona memory"""
        if not self._current_context:
            return

        try:
            interaction_data = {
                'user_input': user_input,
                'response': response,
                'timestamp': datetime.now().isoformat(),
                'session_id': session_id,
                'persona_id': self._active_persona,
                'metadata': metadata or {}
            }

            await self._current_context.memory_layer.store_conversation_context(
                session_id, interaction_data
            )

        except Exception as e:
            logger.error(f"Error storing interaction: {e}")

    async def get_persona_system_instructions(self) -> Optional[str]:
        """Get system instructions from current active persona"""
        if not self._active_persona:
            return None

        try:
            persona = self._personas[self._active_persona]
            if not persona:
                return None

            # Create minimal context for instruction building
            from .base import AgentContext
            context = AgentContext(session_id=self._current_context.session_id if self._current_context else "default")

            return await persona.build_system_instruction(context)

        except Exception as e:
            logger.error(f"Error getting persona system instructions: {e}")
            return None

    async def get_manager_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do manager"""
        stats = {
            "total_personas": len(self._personas),
            "active_persona": self._active_persona,
            "total_switches": self._total_switches,
            "hot_reload_enabled": True,  # Managed by unified ConfigManager
            "last_reload_time": self._last_reload_time,
            "personas_by_capability": {
                cap.value: len(self.list_by_capability(cap))
                for cap in PersonaCapability
            },
            "configuration_system": "unified",
            "memory_integration_enabled": self.memory_manager is not None
        }

        # Add current context info if available
        if self._current_context:
            stats['current_session'] = self._current_context.session_id
            stats['interaction_count'] = self._current_context.current_state.get('interaction_count', 0)

        return stats

    # NOTE: Default personas are now created by the unified ConfigManager
    # in its _create_default_persona_config method

    async def shutdown(self) -> None:
        """Shutdown the manager and clean up resources"""
        logger.info("Shutting down PersonaManager...")

        try:
            # Save current context if exists
            if self._current_context and self.memory_manager:
                await self._current_context.save_state()

            # Remove persona observer from unified config manager
            self.config_manager.remove_persona_observer(self._on_persona_config_change)

            # Stop unified hot-reload (if this is the only user)
            # Note: In production, multiple components may use hot-reload
            # self.config_manager.stop_hot_reload()

            # Deactivate all personas
            for persona in self._personas.values():
                persona.deactivate()

            # Clear storage
            self._personas.clear()
            self._active_persona = None
            self._current_context = None

            logger.info("PersonaManager shutdown complete")

        except Exception as e:
            logger.error(f"Error during PersonaManager shutdown: {e}")

    def __del__(self):
        """Destructor ensures resource cleanup"""
        # No direct observer cleanup needed - handled by unified ConfigManager
        pass