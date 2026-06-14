"""
Tests for issue #685: PersonaManager capabilities mapping fix.

Verifies that capabilities strings from unified PersonaConfig are correctly
mapped to AgentCapability enum values before passing to PydanticPersonaConfig,
so BaseAutonomousPersona is created via the main path instead of the minimal
fallback.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.personas.base import AgentCapability


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_unified_config(capabilities: list):
    """Build a minimal unified PersonaConfig-like object."""
    cfg = MagicMock()
    cfg.persona_id = "test_persona"
    cfg.capabilities = capabilities
    cfg.model_preferences = MagicMock()
    cfg.model_preferences.to_dict.return_value = {}
    cfg.communication_style = MagicMock()
    cfg.communication_style.value = "technical"
    return cfg


def _make_manager():
    """Return a PersonaManager instance bypassing __init__ (all deps mocked)."""
    from deile.personas.manager import PersonaManager

    manager = PersonaManager.__new__(PersonaManager)
    # Provide only the attributes that _create_persona_from_config touches
    manager.loader = MagicMock()
    manager.loader.load_persona_instructions = AsyncMock(
        # system_instruction requires min_length=100
        return_value="You are a test persona for capabilities mapping. " * 3,
    )
    manager.memory_manager = None
    manager._memory_integrated = False
    return manager


class _FakePersona:
    """Minimal stand-in for BaseAutonomousPersona that records the config it received."""

    last_config = None

    def __init__(self, config):
        _FakePersona.last_config = config

    async def initialize(self):
        pass


# ---------------------------------------------------------------------------
# AC1: valid capabilities are mapped correctly
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_valid_capabilities_mapped():
    """capabilities=['code_generation','testing'] → [CODE_GENERATION, TESTING]."""
    manager = _make_manager()

    with patch("deile.personas.manager.BaseAutonomousPersona", _FakePersona):
        unified_cfg = _make_unified_config(["code_generation", "testing"])
        await manager._create_persona_from_config("test_persona", unified_cfg)

    # PersonaConfig has use_enum_values=True, so capabilities are stored as strings
    caps = _FakePersona.last_config.capabilities
    assert "code_generation" in caps
    assert "testing" in caps


# ---------------------------------------------------------------------------
# AC2: unknown string is skipped with WARNING; valid ones remain
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_unknown_capability_skipped_with_warning(caplog):
    """'voar' is invalid → skipped with WARNING; valid strings remain."""
    manager = _make_manager()

    with patch("deile.personas.manager.BaseAutonomousPersona", _FakePersona), \
         caplog.at_level(logging.WARNING, logger="deile.personas.manager"):
        unified_cfg = _make_unified_config(["code_generation", "voar"])
        await manager._create_persona_from_config("test_persona", unified_cfg)

    # PersonaConfig has use_enum_values=True, so capabilities are stored as strings
    caps = _FakePersona.last_config.capabilities
    assert "code_generation" in caps
    assert "voar" not in caps
    assert any("voar" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# AC3a: all-invalid list → default CODE_ANALYSIS + WARNING
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_all_invalid_capabilities_uses_default(caplog):
    """All capabilities invalid → [CODE_ANALYSIS] with WARNING; no ValidationError."""
    manager = _make_manager()

    with patch("deile.personas.manager.BaseAutonomousPersona", _FakePersona), \
         caplog.at_level(logging.WARNING, logger="deile.personas.manager"):
        unified_cfg = _make_unified_config(["voar", "teleportar"])
        await manager._create_persona_from_config("test_persona", unified_cfg)

    # PersonaConfig has use_enum_values=True → stored as string
    caps = _FakePersona.last_config.capabilities
    assert caps == ["code_analysis"]
    assert any("defaulting to CODE_ANALYSIS" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# AC3b: empty list → default CODE_ANALYSIS + WARNING
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_empty_capabilities_uses_default(caplog):
    """Empty capabilities list → [CODE_ANALYSIS] with WARNING; no ValidationError."""
    manager = _make_manager()

    with patch("deile.personas.manager.BaseAutonomousPersona", _FakePersona), \
         caplog.at_level(logging.WARNING, logger="deile.personas.manager"):
        unified_cfg = _make_unified_config([])
        await manager._create_persona_from_config("test_persona", unified_cfg)

    # PersonaConfig has use_enum_values=True → stored as string
    caps = _FakePersona.last_config.capabilities
    assert caps == ["code_analysis"]
    assert any("defaulting to CODE_ANALYSIS" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# AC4: main path (BaseAutonomousPersona) used, NOT _create_minimal_persona
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_main_path_used_not_fallback():
    """Valid capabilities → BaseAutonomousPersona created; _create_minimal_persona NOT called."""
    manager = _make_manager()
    minimal_called = []

    original_minimal = type(manager)._create_minimal_persona

    def spy_minimal(self, *args, **kwargs):
        minimal_called.append(True)
        return original_minimal(self, *args, **kwargs)

    manager._create_minimal_persona = spy_minimal.__get__(manager, type(manager))

    with patch("deile.personas.manager.BaseAutonomousPersona", _FakePersona):
        unified_cfg = _make_unified_config(["code_generation"])
        await manager._create_persona_from_config("test_persona", unified_cfg)

    assert not minimal_called, "_create_minimal_persona should NOT have been called"
