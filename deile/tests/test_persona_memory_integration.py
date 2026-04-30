"""Tests for PersonaMemoryLayer integration with unified memory"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from deile.personas.memory.integration import PersonaMemoryLayer
from deile.memory.memory_manager import MemoryManager
from deile.personas.context import PersonaContext


@pytest.fixture
async def memory_manager():
    """Mock MemoryManager for testing"""
    mock_memory = Mock(spec=MemoryManager)
    mock_memory.working_memory = AsyncMock()
    mock_memory.episodic_memory = AsyncMock()
    mock_memory.semantic_memory = AsyncMock()
    mock_memory.procedural_memory = AsyncMock()
    return mock_memory


@pytest.fixture
async def persona_memory_layer(memory_manager):
    """PersonaMemoryLayer fixture"""
    return PersonaMemoryLayer(memory_manager, "test_persona")


class TestPersonaMemoryLayerIntegration:
    """Test PersonaMemoryLayer integration with unified memory"""

    @pytest.mark.asyncio
    async def test_store_persona_state(self, persona_memory_layer, memory_manager):
        """Test storing persona state in unified memory"""
        # Arrange
        test_state = {"key": "value", "timestamp": datetime.now().isoformat()}

        # Act
        await persona_memory_layer.store_persona_state(test_state)

        # Assert
        memory_manager.semantic_memory.store_concept.assert_called_once()
        call_args = memory_manager.semantic_memory.store_concept.call_args

        assert call_args[1]['concept'] == "persona:test_persona:state"
        assert call_args[1]['data']['state'] == test_state
        assert call_args[1]['data']['persona_id'] == "test_persona"
        assert call_args[1]['metadata']['type'] == 'persona_state'

    @pytest.mark.asyncio
    async def test_get_persona_state(self, persona_memory_layer, memory_manager):
        """Test retrieving persona state from unified memory"""
        # Arrange
        expected_state = {"key": "value"}
        memory_manager.semantic_memory.get_concept.return_value = {
            'state': expected_state
        }

        # Act
        result = await persona_memory_layer.get_persona_state()

        # Assert
        memory_manager.semantic_memory.get_concept.assert_called_once_with(
            "persona:test_persona:state"
        )
        assert result == expected_state

    @pytest.mark.asyncio
    async def test_get_persona_state_not_found(self, persona_memory_layer, memory_manager):
        """Test retrieving persona state when none exists"""
        # Arrange
        memory_manager.semantic_memory.get_concept.return_value = None

        # Act
        result = await persona_memory_layer.get_persona_state()

        # Assert
        assert result == {}

    @pytest.mark.asyncio
    async def test_store_conversation_context(self, persona_memory_layer, memory_manager):
        """Test storing conversation context in episodic memory"""
        # Arrange
        session_id = "test_session"
        context = {"interaction": "test"}

        # Act
        await persona_memory_layer.store_conversation_context(session_id, context)

        # Assert
        memory_manager.episodic_memory.record_event.assert_called_once()
        call_args = memory_manager.episodic_memory.record_event.call_args

        assert call_args[1]['event_type'] == "persona_conversation"
        assert call_args[1]['session_id'] == session_id
        assert call_args[1]['details']['persona_id'] == "test_persona"
        assert call_args[1]['details']['context'] == context

    @pytest.mark.asyncio
    async def test_get_conversation_history(self, persona_memory_layer, memory_manager):
        """Test retrieving conversation history"""
        # Arrange
        session_id = "test_session"
        mock_events = [
            {'details': {'persona_id': 'test_persona', 'context': {'msg': '1'}}},
            {'details': {'persona_id': 'other_persona', 'context': {'msg': '2'}}},
            {'details': {'persona_id': 'test_persona', 'context': {'msg': '3'}}}
        ]
        memory_manager.episodic_memory.get_session_events.return_value = mock_events

        # Act
        result = await persona_memory_layer.get_conversation_history(session_id, limit=10)

        # Assert
        memory_manager.episodic_memory.get_session_events.assert_called_once_with(
            session_id=session_id,
            event_type="persona_conversation",
            limit=10
        )
        # Should filter only events for test_persona
        assert len(result) == 2
        assert all(event['details']['persona_id'] == 'test_persona' for event in result)

    @pytest.mark.asyncio
    async def test_preference_operations(self, persona_memory_layer, memory_manager):
        """Test persona preference storage and retrieval"""
        # Test store preference
        await persona_memory_layer.store_persona_preference("test_key", "test_value")
        memory_manager.working_memory.set.assert_called_with(
            key="persona:test_persona:pref:test_key",
            value="test_value",
            ttl=3600
        )

        # Test get preference
        memory_manager.working_memory.get.return_value = "test_value"
        result = await persona_memory_layer.get_persona_preference("test_key")
        assert result == "test_value"

        # Test get preference with default
        memory_manager.working_memory.get.return_value = None
        result = await persona_memory_layer.get_persona_preference("missing_key", "default")
        assert result == "default"

    @pytest.mark.asyncio
    async def test_learn_pattern(self, persona_memory_layer, memory_manager):
        """Test pattern learning in procedural memory"""
        # Arrange
        pattern_type = "test_pattern"
        pattern_data = {"pattern": "data"}
        success_rate = 0.85

        # Act
        await persona_memory_layer.learn_pattern(pattern_type, pattern_data, success_rate)

        # Assert
        memory_manager.procedural_memory.learn_pattern.assert_called_once_with(
            pattern_type=f"persona:test_persona:{pattern_type}",
            context=pattern_data,
            success_metrics={'success_rate': success_rate}
        )

    @pytest.mark.asyncio
    async def test_get_learned_patterns(self, persona_memory_layer, memory_manager):
        """Test retrieving learned patterns"""
        # Arrange
        pattern_type = "test_pattern"
        expected_patterns = [{"pattern": "data"}]
        memory_manager.procedural_memory.get_patterns.return_value = expected_patterns

        # Act
        result = await persona_memory_layer.get_learned_patterns(pattern_type)

        # Assert
        memory_manager.procedural_memory.get_patterns.assert_called_once_with(
            pattern_type=f"persona:test_persona:{pattern_type}"
        )
        assert result == expected_patterns

    @pytest.mark.asyncio
    async def test_cleanup_persona_memory(self, persona_memory_layer, memory_manager):
        """Test persona memory cleanup"""
        # Act
        await persona_memory_layer.cleanup_persona_memory()

        # Assert
        memory_manager.working_memory.remove_pattern.assert_called_once_with(
            "persona:test_persona:*"
        )
        memory_manager.semantic_memory.mark_for_cleanup.assert_called_once_with(
            pattern="persona:test_persona:*"
        )


class TestPersonaContextIntegration:
    """Test PersonaContext integration with unified memory"""

    @pytest.mark.asyncio
    async def test_persona_context_creation(self):
        """Test PersonaContext creation with memory integration"""
        # Arrange
        mock_memory = Mock(spec=MemoryManager)
        persona_id = "test_persona"
        session_id = "test_session"

        with patch.object(PersonaMemoryLayer, '__init__', return_value=None) as mock_init:
            mock_layer = Mock(spec=PersonaMemoryLayer)
            mock_layer.get_persona_state = AsyncMock(return_value={'key': 'value'})
            mock_layer.get_persona_preference = AsyncMock(return_value=None)

            # Act
            with patch('deile.personas.context.PersonaMemoryLayer', return_value=mock_layer):
                context = await PersonaContext.create(persona_id, session_id, mock_memory)

            # Assert
            assert context.persona_id == persona_id
            assert context.session_id == session_id
            assert context.current_state == {'key': 'value'}

    @pytest.mark.asyncio
    async def test_persona_context_save_state(self):
        """Test PersonaContext state saving"""
        # Arrange
        mock_layer = Mock(spec=PersonaMemoryLayer)
        mock_layer.store_persona_state = AsyncMock()
        mock_layer.store_persona_preference = AsyncMock()

        context = PersonaContext(
            persona_id="test",
            session_id="session",
            memory_layer=mock_layer,
            current_state={'state': 'data'},
            preferences={'pref': 'value'}
        )

        # Act
        await context.save_state()

        # Assert
        mock_layer.store_persona_state.assert_called_once_with({'state': 'data'})
        mock_layer.store_persona_preference.assert_called_once_with('pref', 'value')


class TestMemoryConsistency:
    """Test memory consistency after duplicate removal"""

    @pytest.mark.asyncio
    async def test_no_duplicate_memory_systems(self):
        """Ensure no duplicate memory systems exist"""
        # This test ensures old memory files don't exist
        import os
        from pathlib import Path

        # Check that old duplicate files are removed
        old_memory_files = [
            'deile/personas/memory/persistent.py',
            'deile/personas/memory/working.py',
            'deile/personas/memory/models.py'
        ]

        for file_path in old_memory_files:
            assert not Path(file_path).exists(), f"Duplicate memory file still exists: {file_path}"

    @pytest.mark.asyncio
    async def test_persona_memory_uses_unified_system(self):
        """Test that persona memory operations use unified memory system"""
        # Arrange
        mock_memory = Mock(spec=MemoryManager)
        mock_memory.semantic_memory = AsyncMock()
        mock_memory.episodic_memory = AsyncMock()
        mock_memory.working_memory = AsyncMock()
        mock_memory.procedural_memory = AsyncMock()

        persona_layer = PersonaMemoryLayer(mock_memory, "test")

        # Act & Assert - ensure all operations go through unified memory
        await persona_layer.store_persona_state({"test": "data"})
        mock_memory.semantic_memory.store_concept.assert_called_once()

        await persona_layer.store_conversation_context("session", {"ctx": "data"})
        mock_memory.episodic_memory.record_event.assert_called_once()

        await persona_layer.store_persona_preference("key", "value")
        mock_memory.working_memory.set.assert_called_once()

        await persona_layer.learn_pattern("pattern", {}, 0.8)
        mock_memory.procedural_memory.learn_pattern.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_operations_are_persona_scoped(self):
        """Test that memory operations are properly scoped to persona"""
        # Arrange
        mock_memory = Mock(spec=MemoryManager)
        mock_memory.semantic_memory = AsyncMock()
        mock_memory.working_memory = AsyncMock()
        mock_memory.procedural_memory = AsyncMock()

        persona_layer = PersonaMemoryLayer(mock_memory, "test_persona")

        # Act
        await persona_layer.store_persona_state({"state": "data"})
        await persona_layer.store_persona_preference("pref", "value")
        await persona_layer.learn_pattern("pattern_type", {}, 0.9)

        # Assert - all operations include persona scoping
        semantic_call = mock_memory.semantic_memory.store_concept.call_args
        assert "persona:test_persona:state" in semantic_call[1]['concept']

        working_call = mock_memory.working_memory.set.call_args
        assert "persona:test_persona:pref:" in working_call[1]['key']

        procedural_call = mock_memory.procedural_memory.learn_pattern.call_args
        assert "persona:test_persona:pattern_type" in procedural_call[1]['pattern_type']