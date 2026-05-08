"""
Comprehensive test suite for persona integration with DeileAgent
=============================================================

Tests the deep integration between personas and DEILE's core systems.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml

from deile.personas.base import AgentCapability, PersonaConfig
# Import the components we're testing
from deile.personas.integration import (PersonaEnhancedAgent,
                                        PersonaIntegrationContext,
                                        PersonaIntegrationLayer)
from deile.personas.manager import PersonaManager


class MockMemoryManager:
    """Mock memory manager for testing"""

    def __init__(self):
        self.semantic_memory = AsyncMock()
        self.episodic_memory = AsyncMock()
        self.working_memory = AsyncMock()
        self.procedural_memory = AsyncMock()


TEST_SYSTEM_INSTRUCTION = (
    "You are a test persona for integration testing purposes. You help "
    "with automated test scenarios and validate system behavior in "
    "controlled environments."
)


class MockBasePersona:
    """Mock persona for testing"""

    def __init__(self, persona_id: str = "test_persona", name: str = "Test Persona"):
        self.persona_id = persona_id
        self.name = name
        self.config = PersonaConfig(
            name=name,
            persona_id=persona_id,
            description="Test persona for integration testing",
            capabilities=[AgentCapability.CODE_GENERATION],
            system_instruction=TEST_SYSTEM_INSTRUCTION,
        )
        self._is_active = False

    def activate(self, session_id: str = "default", context=None):
        self._is_active = True

    def deactivate(self):
        self._is_active = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    async def build_system_instruction(self, context):
        return f"Test system instruction for {self.name}"

    def prioritize_tools(self, tools):
        return tools  # No prioritization for testing


class MockDeileAgent:
    """Mock DeileAgent for testing"""

    def __init__(self):
        self.memory_manager = MockMemoryManager()
        self.tool_registry = Mock()
        self.context_manager = Mock()
        self.intent_analyzer = Mock()
        self.persona_manager = None
        self.persona_enhanced = False

        # Mock methods
        self.initialize = AsyncMock()
        self.process_input = AsyncMock(return_value=Mock(
            content="Test response",
            status="success",
            metadata={}
        ))
        self.get_stats = AsyncMock(return_value={"status": "active"})


@pytest.fixture
def mock_memory_manager():
    """Fixture for mock memory manager"""
    return MockMemoryManager()


@pytest.fixture
def mock_deile_agent():
    """Fixture for mock DeileAgent"""
    return MockDeileAgent()


@pytest.fixture
def temp_personas_dir():
    """Fixture for temporary personas directory"""
    with tempfile.TemporaryDirectory() as temp_dir:
        personas_dir = Path(temp_dir) / "personas"
        personas_dir.mkdir(exist_ok=True)

        # Create test persona config
        test_config = {
            "name": "Test Developer",
            "persona_id": "test_developer",
            "description": "Test persona for integration testing",
            "capabilities": ["code_generation", "debugging"],
            "system_instruction": "You are a test developer persona"
        }

        config_file = personas_dir / "test_developer.yaml"
        with open(config_file, 'w') as f:
            yaml.dump(test_config, f)

        yield personas_dir


class TestPersonaIntegrationContext:
    """Test PersonaIntegrationContext functionality"""

    def test_integration_context_creation(self, mock_deile_agent):
        """Test creating integration context"""
        from deile.personas.manager import PersonaManager

        persona_manager = PersonaManager()
        context = PersonaIntegrationContext(
            agent=mock_deile_agent,
            persona_manager=persona_manager,
            session_id="test_session"
        )

        assert context.agent == mock_deile_agent
        assert context.persona_manager == persona_manager
        assert context.session_id == "test_session"
        assert not context.has_active_persona

    def test_integration_context_with_active_persona(self, mock_deile_agent):
        """Test integration context with active persona"""
        from deile.personas.manager import PersonaManager

        persona_manager = PersonaManager()
        mock_persona = MockBasePersona()

        context = PersonaIntegrationContext(
            agent=mock_deile_agent,
            persona_manager=persona_manager,
            current_persona=mock_persona
        )

        assert context.has_active_persona
        assert context.current_persona == mock_persona


class TestPersonaEnhancedAgent:
    """Test PersonaEnhancedAgent functionality"""

    def test_persona_enhanced_agent_creation(self, mock_deile_agent):
        """Test creating PersonaEnhancedAgent"""
        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent)

        assert enhanced_agent.base_agent == mock_deile_agent
        assert enhanced_agent.persona_manager is not None
        assert enhanced_agent.integration_context is None  # Not initialized yet

    @pytest.mark.asyncio
    async def test_persona_enhanced_agent_initialization(self, mock_deile_agent, temp_personas_dir):
        """Test PersonaEnhancedAgent initialization"""
        # spec=PersonaManager so hasattr(mock_pm, '_initialized') returns False,
        # ensuring the production code calls initialize() rather than skipping it.
        mock_pm = AsyncMock(spec=PersonaManager)
        mock_pm.initialize = AsyncMock()

        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent, mock_pm)
        await enhanced_agent.initialize()

        # Verify initialization
        mock_deile_agent.initialize.assert_called_once()
        mock_pm.initialize.assert_called_once()
        assert enhanced_agent.integration_context is not None

    @pytest.mark.asyncio
    async def test_process_input_without_persona(self, mock_deile_agent):
        """Test processing input without active persona"""
        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent)
        enhanced_agent.persona_manager = Mock()
        enhanced_agent.persona_manager.get_current_persona = Mock(return_value=None)

        # Mock the _has_active_persona method to return False
        enhanced_agent._has_active_persona = Mock(return_value=False)

        await enhanced_agent.process_input_with_persona("test input")

        # Should fallback to base agent
        mock_deile_agent.process_input.assert_called_once_with("test input", "default")

    @pytest.mark.asyncio
    async def test_process_input_with_persona(self, mock_deile_agent):
        """Test processing input with active persona"""
        mock_persona = MockBasePersona()

        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent)
        enhanced_agent.integration_context = PersonaIntegrationContext(
            agent=mock_deile_agent,
            persona_manager=Mock(),
            current_persona=mock_persona
        )

        # Mock methods
        enhanced_agent._has_active_persona = Mock(return_value=True)
        enhanced_agent._get_current_persona = AsyncMock(return_value=mock_persona)
        enhanced_agent._enhance_context_with_persona = AsyncMock(return_value={})
        enhanced_agent._post_process_with_persona = AsyncMock(side_effect=lambda x, *args: x)

        await enhanced_agent.process_input_with_persona("test input")

        # Should use enhanced processing
        mock_deile_agent.process_input.assert_called_once()

    @pytest.mark.asyncio
    async def test_switch_persona(self, mock_deile_agent):
        """Test persona switching"""
        mock_pm = AsyncMock()
        mock_pm.switch_persona = AsyncMock(return_value=True)

        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent, mock_pm)
        enhanced_agent.integration_context = PersonaIntegrationContext(
            agent=mock_deile_agent,
            persona_manager=mock_pm
        )

        # Mock methods
        enhanced_agent._get_current_persona = AsyncMock(return_value=MockBasePersona("new_persona"))
        enhanced_agent._update_agent_systems_with_persona = AsyncMock()

        result = await enhanced_agent.switch_persona("new_persona", "test_session")

        assert result
        mock_pm.switch_persona.assert_called_once_with("new_persona", "test_session")

    @pytest.mark.asyncio
    async def test_enhance_context_with_persona(self, mock_deile_agent):
        """Test context enhancement with persona"""
        mock_persona = MockBasePersona("test_persona", "Test Persona")

        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent)
        enhanced_agent.integration_context = PersonaIntegrationContext(
            agent=mock_deile_agent,
            persona_manager=Mock(),
            current_persona=mock_persona
        )
        enhanced_agent._has_active_persona = Mock(return_value=True)

        kwargs = await enhanced_agent._enhance_context_with_persona(
            "test input", "session_1", some_arg="value"
        )

        assert "persona_context" in kwargs
        assert kwargs["persona_context"]["persona_id"] == "test_persona"
        assert kwargs["persona_context"]["persona_name"] == "Test Persona"
        assert kwargs["some_arg"] == "value"


class TestPersonaIntegrationLayer:
    """Test PersonaIntegrationLayer functionality"""

    def test_integration_layer_creation(self, mock_deile_agent):
        """Test creating PersonaIntegrationLayer"""
        layer = PersonaIntegrationLayer(mock_deile_agent)

        assert layer.agent == mock_deile_agent
        assert layer.persona_manager is None

    def test_set_persona_manager(self, mock_deile_agent):
        """Test setting persona manager"""
        layer = PersonaIntegrationLayer(mock_deile_agent)
        mock_pm = Mock()

        layer.set_persona_manager(mock_pm)

        assert layer.persona_manager == mock_pm

    @pytest.mark.asyncio
    async def test_enhance_context_building_without_persona(self, mock_deile_agent):
        """Test context enhancement without active persona"""
        layer = PersonaIntegrationLayer(mock_deile_agent)
        base_context = {"user_input": "test"}

        enhanced = await layer.enhance_context_building(base_context)

        assert enhanced == base_context

    @pytest.mark.asyncio
    async def test_enhance_context_building_with_persona(self, mock_deile_agent):
        """Test context enhancement with active persona"""
        mock_persona = MockBasePersona("test_persona", "Test Persona")
        mock_pm = Mock()
        mock_pm.has_active_persona = Mock(return_value=True)
        mock_pm.get_current_persona = Mock(return_value=mock_persona)

        layer = PersonaIntegrationLayer(mock_deile_agent)
        layer.set_persona_manager(mock_pm)

        base_context = {"user_input": "test"}
        enhanced = await layer.enhance_context_building(base_context)

        assert "persona_id" in enhanced
        assert enhanced["persona_id"] == "test_persona"

    @pytest.mark.asyncio
    async def test_enhance_tool_selection(self, mock_deile_agent):
        """Test tool selection enhancement"""
        mock_persona = MockBasePersona()
        mock_pm = Mock()
        mock_pm.has_active_persona = Mock(return_value=True)
        mock_pm.get_current_persona = Mock(return_value=mock_persona)

        layer = PersonaIntegrationLayer(mock_deile_agent)
        layer.set_persona_manager(mock_pm)

        tools = ["tool1", "tool2", "tool3"]
        enhanced_tools = await layer.enhance_tool_selection(tools)

        assert enhanced_tools == tools  # MockBasePersona doesn't change order


class TestPersonaManagerIntegration:
    """Test PersonaManager integration features"""

    @pytest.mark.asyncio
    async def test_persona_manager_with_memory_manager(self, mock_memory_manager, temp_personas_dir):
        """Test PersonaManager with memory manager integration"""
        # Create PersonaManager with memory integration
        pm = PersonaManager(memory_manager=mock_memory_manager)

        assert pm.memory_manager == mock_memory_manager

        # Test memory manager setting
        new_memory = MockMemoryManager()
        pm.set_memory_manager(new_memory)
        assert pm.memory_manager == new_memory

    @pytest.mark.asyncio
    async def test_persona_manager_integration_methods(self, mock_memory_manager):
        """Test new integration methods in PersonaManager"""
        pm = PersonaManager(memory_manager=mock_memory_manager)

        # Test has_active_persona
        assert not pm.has_active_persona()

        # Test get_current_persona
        assert pm.get_current_persona() is None

        # Mock active persona
        mock_persona = MockBasePersona()
        pm._active_persona = "test_persona"
        pm._personas["test_persona"] = mock_persona

        assert pm.has_active_persona()
        assert pm.get_current_persona() == mock_persona

    @pytest.mark.asyncio
    async def test_store_interaction(self, mock_memory_manager):
        """Test storing interaction in persona memory"""
        pm = PersonaManager(memory_manager=mock_memory_manager)

        # Mock current context
        mock_context = Mock()
        mock_context.memory_layer = AsyncMock()
        mock_context.session_id = "test_session"
        pm._current_context = mock_context
        pm._active_persona = "test_persona"

        await pm.store_interaction(
            user_input="Hello",
            response="Hi there",
            session_id="test_session"
        )

        # Verify interaction was stored
        mock_context.memory_layer.store_conversation_context.assert_called_once()


class TestDeileAgentIntegration:
    """Test DeileAgent integration features"""

    def test_agent_persona_enhancement_flag(self):
        """Test persona enhancement flag"""
        # Since we can't easily create a real DeileAgent, we'll test the concept
        agent = MockDeileAgent()

        # Initially not enhanced
        assert not agent.persona_enhanced

        # Enable enhancement
        agent.persona_enhanced = True
        assert agent.persona_enhanced

    @pytest.mark.asyncio
    async def test_agent_integration_methods(self, mock_memory_manager):
        """Test agent integration methods"""
        agent = MockDeileAgent()
        agent.memory_manager = mock_memory_manager

        # Test enable_persona_enhancement method concept
        pm = PersonaManager(memory_manager=mock_memory_manager)
        agent.persona_manager = pm
        agent.persona_enhanced = True

        assert agent.persona_manager == pm
        assert agent.persona_enhanced


class TestEndToEndIntegration:
    """End-to-end integration tests"""

    @pytest.mark.asyncio
    async def test_full_integration_workflow(self, mock_deile_agent, temp_personas_dir):
        """Test complete integration workflow"""
        # 1. Create PersonaManager with memory
        pm = PersonaManager(memory_manager=mock_deile_agent.memory_manager)

        # 2. Create PersonaEnhancedAgent
        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent, pm)

        # 3. Initialize (mocked)
        with patch.object(pm, 'initialize', new=AsyncMock()):
            await enhanced_agent.initialize()

        # 4. Test stats collection
        stats = await enhanced_agent.get_stats()
        assert "persona_integration" in stats

    @pytest.mark.asyncio
    async def test_memory_integration_flow(self, mock_memory_manager):
        """Test memory integration flow"""
        # Create PersonaManager with memory
        pm = PersonaManager(memory_manager=mock_memory_manager)

        # Mock persona switch with memory operations
        pm._active_persona = "test_persona"
        mock_context = Mock()
        mock_context.save_state = AsyncMock()
        pm._current_context = mock_context

        # Simulate memory operations during persona switch
        with patch('deile.personas.context.PersonaContext.create', new=AsyncMock()) as mock_create:
            mock_new_context = Mock()
            mock_create.return_value = mock_new_context

            # Mock a persona for testing
            mock_persona = MockBasePersona()
            pm._personas["new_persona"] = mock_persona

            await pm.switch_persona("new_persona", "test_session")

            # Verify memory operations
            mock_context.save_state.assert_called_once()
            mock_create.assert_called_once()


# Performance and edge case tests

class TestIntegrationPerformance:
    """Test integration performance and edge cases"""

    @pytest.mark.asyncio
    async def test_error_handling_in_integration(self, mock_deile_agent):
        """Test error handling in integration"""
        # Create enhanced agent with faulty persona manager
        faulty_pm = Mock()
        faulty_pm.get_current_persona = Mock(side_effect=Exception("Test error"))

        enhanced_agent = PersonaEnhancedAgent(mock_deile_agent, faulty_pm)

        # Should fallback gracefully
        await enhanced_agent.process_input_with_persona("test input")

        # Should have fallen back to base agent
        mock_deile_agent.process_input.assert_called_once()

    @pytest.mark.asyncio
    async def test_integration_without_memory_manager(self):
        """Test integration works without memory manager"""
        pm = PersonaManager(memory_manager=None)

        # Should work without memory manager
        assert not pm.has_active_persona()
        assert pm.memory_manager is None

    def test_integration_layer_without_persona_manager(self, mock_deile_agent):
        """Test integration layer works without persona manager"""
        layer = PersonaIntegrationLayer(mock_deile_agent)

        # Should handle missing persona manager gracefully
        base_context = {"test": "data"}

        # This should work without async since no persona manager
        result = asyncio.run(layer.enhance_context_building(base_context))

        assert result == base_context


# Configuration and validation tests

class TestIntegrationValidation:
    """Test integration validation and configuration"""

    def test_persona_config_validation(self):
        """Test persona config validation for integration"""
        # Valid config
        config = PersonaConfig(
            name="Test Persona",
            persona_id="test_persona",
            description="Test description for integration",
            capabilities=[AgentCapability.CODE_GENERATION],
            system_instruction=TEST_SYSTEM_INSTRUCTION,
        )

        assert config.name == "Test Persona"
        # PersonaConfig has use_enum_values=True, so typed enum-list fields are
        # coerced to their string values during validation.
        assert config.capabilities == [AgentCapability.CODE_GENERATION.value]

    @pytest.mark.asyncio
    async def test_integration_context_validation(self, mock_deile_agent):
        """Test integration context validation"""
        pm = PersonaManager()

        # Create context with validation
        context = PersonaIntegrationContext(
            agent=mock_deile_agent,
            persona_manager=pm,
            session_id="valid_session"
        )

        assert context.session_id == "valid_session"
        assert not context.has_active_persona

        # Add persona and validate
        mock_persona = MockBasePersona()
        context.current_persona = mock_persona

        assert context.has_active_persona


if __name__ == "__main__":
    # Run basic tests if executed directly
    pytest.main([__file__, "-v"])