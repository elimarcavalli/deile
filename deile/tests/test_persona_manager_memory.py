"""Tests for PersonaManager memory integration"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from deile.personas.manager import PersonaManager
from deile.personas.context import PersonaContext
from deile.personas.memory.integration import PersonaMemoryLayer
from deile.memory.memory_manager import MemoryManager
from deile.personas.base import BasePersona


class TestPersonaManagerMemoryIntegration:
    """Test PersonaManager integration with unified memory"""

    @pytest.fixture
    async def mock_memory_manager(self):
        """Mock MemoryManager for testing"""
        mock_memory = Mock(spec=MemoryManager)
        mock_memory.working_memory = AsyncMock()
        mock_memory.episodic_memory = AsyncMock()
        mock_memory.semantic_memory = AsyncMock()
        mock_memory.procedural_memory = AsyncMock()
        return mock_memory

    @pytest.fixture
    async def persona_manager(self, mock_memory_manager):
        """PersonaManager fixture with memory integration"""
        manager = PersonaManager(memory_manager=mock_memory_manager)
        return manager

    @pytest.mark.asyncio
    async def test_persona_manager_uses_unified_memory(self, persona_manager, mock_memory_manager):
        """Test that PersonaManager uses unified memory"""
        # Assert
        assert persona_manager.memory_manager is mock_memory_manager

    @pytest.mark.asyncio
    async def test_set_memory_manager(self, persona_manager):
        """Test setting memory manager"""
        # Arrange
        new_memory_manager = Mock(spec=MemoryManager)

        # Act
        persona_manager.set_memory_manager(new_memory_manager)

        # Assert
        assert persona_manager.memory_manager is new_memory_manager

    @pytest.mark.asyncio
    async def test_switch_persona_memory_operations(self, persona_manager, mock_memory_manager):
        """Test persona switching with memory operations"""
        # Arrange
        mock_persona = Mock(spec=BasePersona)
        mock_persona.name = "Test Persona"
        mock_persona.activate = Mock()
        mock_persona.deactivate = Mock()

        persona_manager._personas = {
            'test_persona': mock_persona
        }

        with patch.object(PersonaContext, 'create') as mock_create:
            mock_context = Mock(spec=PersonaContext)
            mock_context.save_state = AsyncMock()
            mock_create.return_value = mock_context

            # Mock current context
            persona_manager._current_context = Mock(spec=PersonaContext)
            persona_manager._current_context.save_state = AsyncMock()

            # Act
            result = await persona_manager.switch_persona('test_persona', 'test_session')

            # Assert
            assert result is True

            # Assert - old context saved
            persona_manager._current_context.save_state.assert_called_once()

            # Assert - new context created with memory manager
            mock_create.assert_called_once_with(
                persona_id='test_persona',
                session_id='test_session',
                memory_manager=mock_memory_manager
            )

            # Assert - persona switch event recorded
            mock_memory_manager.episodic_memory.record_event.assert_called_once()
            call_args = mock_memory_manager.episodic_memory.record_event.call_args
            assert call_args[1]['event_type'] == "persona_switch"
            assert call_args[1]['session_id'] == "test_session"

    @pytest.mark.asyncio
    async def test_switch_persona_without_memory_manager(self, mock_memory_manager):
        """Test persona switching without memory manager"""
        # Arrange
        manager = PersonaManager(memory_manager=None)
        mock_persona = Mock(spec=BasePersona)
        mock_persona.name = "Test Persona"
        mock_persona.activate = Mock()
        mock_persona.deactivate = Mock()

        manager._personas = {
            'test_persona': mock_persona
        }

        # Act
        result = await manager.switch_persona('test_persona', 'test_session')

        # Assert
        assert result is True
        assert manager._current_context is None
        mock_persona.activate.assert_called_once()

    @pytest.mark.asyncio
    async def test_enhance_context(self, persona_manager, mock_memory_manager):
        """Test context enhancement with persona memory"""
        # Arrange
        base_context = {"base": "data"}

        # Create mock context with memory layer
        mock_memory_layer = Mock(spec=PersonaMemoryLayer)
        mock_memory_layer.get_conversation_history = AsyncMock(return_value=[{"msg": "test"}])
        mock_memory_layer.get_learned_patterns = AsyncMock(return_value=[{"pattern": "learned"}])

        mock_context = PersonaContext(
            persona_id="test_persona",
            session_id="test_session",
            memory_layer=mock_memory_layer,
            current_state={"state": "current"},
            preferences={"pref": "value"}
        )

        persona_manager._active_persona = "test_persona"
        persona_manager._current_context = mock_context

        # Act
        enhanced_context = await persona_manager.enhance_context(base_context)

        # Assert
        assert enhanced_context["base"] == "data"  # Original context preserved
        assert enhanced_context["persona_id"] == "test_persona"
        assert enhanced_context["persona_state"] == {"state": "current"}
        assert enhanced_context["persona_preferences"] == {"pref": "value"}
        assert enhanced_context["conversation_history"] == [{"msg": "test"}]
        assert enhanced_context["learned_patterns"] == [{"pattern": "learned"}]

    @pytest.mark.asyncio
    async def test_enhance_context_without_active_persona(self, persona_manager):
        """Test context enhancement without active persona"""
        # Arrange
        base_context = {"base": "data"}

        # Act
        enhanced_context = await persona_manager.enhance_context(base_context)

        # Assert - should return original context unchanged
        assert enhanced_context == base_context

    @pytest.mark.asyncio
    async def test_process_interaction_feedback(self, persona_manager, mock_memory_manager):
        """Test processing interaction feedback for learning"""
        # Arrange
        interaction_data = {"user_input": "test", "response": "output"}
        success_metrics = {"success_rate": 0.8}

        # Create mock context with memory layer
        mock_memory_layer = Mock(spec=PersonaMemoryLayer)
        mock_memory_layer.learn_pattern = AsyncMock()

        mock_context = Mock(spec=PersonaContext)
        mock_context.update_from_interaction = AsyncMock()
        mock_context.memory_layer = mock_memory_layer

        persona_manager._current_context = mock_context

        # Act
        await persona_manager.process_interaction_feedback(interaction_data, success_metrics)

        # Assert
        mock_context.update_from_interaction.assert_called_once_with(interaction_data)
        mock_memory_layer.learn_pattern.assert_called_once_with(
            pattern_type="successful_interaction",
            pattern_data=interaction_data,
            success_rate=0.8
        )

    @pytest.mark.asyncio
    async def test_process_interaction_feedback_low_success_rate(self, persona_manager):
        """Test processing feedback with low success rate (no learning)"""
        # Arrange
        interaction_data = {"user_input": "test", "response": "output"}
        success_metrics = {"success_rate": 0.5}

        mock_context = Mock(spec=PersonaContext)
        mock_context.update_from_interaction = AsyncMock()
        mock_memory_layer = Mock(spec=PersonaMemoryLayer)
        mock_memory_layer.learn_pattern = AsyncMock()
        mock_context.memory_layer = mock_memory_layer

        persona_manager._current_context = mock_context

        # Act
        await persona_manager.process_interaction_feedback(interaction_data, success_metrics)

        # Assert
        mock_context.update_from_interaction.assert_called_once_with(interaction_data)
        # Should not learn pattern due to low success rate
        mock_memory_layer.learn_pattern.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_persona_memory(self, persona_manager, mock_memory_manager):
        """Test persona memory cleanup"""
        # Arrange
        persona_id = "test_persona"

        with patch.object(PersonaMemoryLayer, '__init__', return_value=None):
            mock_layer = Mock(spec=PersonaMemoryLayer)
            mock_layer.cleanup_persona_memory = AsyncMock()

            with patch('deile.personas.manager.PersonaMemoryLayer', return_value=mock_layer):
                # Act
                await persona_manager.cleanup_persona_memory(persona_id)

                # Assert
                mock_layer.cleanup_persona_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_persona_memory_without_memory_manager(self):
        """Test persona memory cleanup without memory manager"""
        # Arrange
        manager = PersonaManager(memory_manager=None)
        persona_id = "test_persona"

        # Act (should not raise exception)
        await manager.cleanup_persona_memory(persona_id)

        # No assertions needed - test passes if no exception is raised

    @pytest.mark.asyncio
    async def test_get_manager_stats_with_memory_integration(self, persona_manager, mock_memory_manager):
        """Test manager stats include memory integration status"""
        # Arrange
        mock_context = PersonaContext(
            persona_id="test_persona",
            session_id="test_session",
            memory_layer=Mock(spec=PersonaMemoryLayer),
            current_state={"interaction_count": 5}
        )
        persona_manager._current_context = mock_context

        # Act
        stats = await persona_manager.get_manager_stats()

        # Assert
        assert stats["memory_integration_enabled"] is True
        assert stats["current_session"] == "test_session"
        assert stats["interaction_count"] == 5

    @pytest.mark.asyncio
    async def test_get_manager_stats_without_memory_integration(self):
        """Test manager stats without memory integration"""
        # Arrange
        manager = PersonaManager(memory_manager=None)

        # Act
        stats = await manager.get_manager_stats()

        # Assert
        assert stats["memory_integration_enabled"] is False
        assert "current_session" not in stats
        assert "interaction_count" not in stats

    @pytest.mark.asyncio
    async def test_shutdown_with_memory_cleanup(self, persona_manager, mock_memory_manager):
        """Test shutdown saves current context"""
        # Arrange
        mock_context = Mock(spec=PersonaContext)
        mock_context.save_state = AsyncMock()
        persona_manager._current_context = mock_context

        # Act
        await persona_manager.shutdown()

        # Assert
        mock_context.save_state.assert_called_once()
        assert persona_manager._current_context is None

    @pytest.mark.asyncio
    async def test_shutdown_without_context(self, persona_manager):
        """Test shutdown without current context"""
        # Arrange
        persona_manager._current_context = None

        # Act (should not raise exception)
        await persona_manager.shutdown()

        # No assertions needed - test passes if no exception is raised