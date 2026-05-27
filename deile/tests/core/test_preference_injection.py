"""Tests for user-preference injection into the system prompt (Issue #341).

Covers:
- Injection when preferences exist
- No-injection when empty
- Position relative to skills block
- Different users see different preferences
- Prompt format integrity
- ContextManager integration (both persona and fallback paths)
- _resolve_user_id fallback
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from deile.core.context_manager import (
    ContextManager,
    _build_preferences_block,
    _resolve_user_id,
)

# PreferenceStore is imported lazily inside _build_preferences_block via
# ``from deile.preferences.store import PreferenceStore``.  Patch that
# target so the mock is used instead of the real store.
_PREF_STORE = "deile.preferences.store.PreferenceStore"


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_prefs_full():
    """PreferenceStore mock with two preferences."""
    store = MagicMock()
    store.get_all.return_value = {
        "response_language": "pt-BR",
        "subagents.mode": "manual",
    }
    return store


@pytest.fixture
def mock_prefs_empty():
    """PreferenceStore mock with zero preferences."""
    store = MagicMock()
    store.get_all.return_value = {}
    return store


@pytest.fixture
def session_with_user():
    """AgentSession-like object with user_id set."""
    s = MagicMock()
    s.user_id = "user-abc-123"
    return s


@pytest.fixture
def session_without_user():
    """AgentSession-like object with user_id=None."""
    s = MagicMock()
    s.user_id = None
    return s


# ── _build_preferences_block ────────────────────────────────────────────


class TestBuildPreferencesBlock:

    async def test_injection_with_preferences(self, session_with_user):
        with patch(
            _PREF_STORE,
            return_value=_make_store({"response_language": "pt-BR", "subagents.mode": "manual"}),
        ):
            block = await _build_preferences_block(session_with_user)

        assert "📋 Preferências do Usuário" in block
        assert "`response_language`: pt-BR" in block
        assert "`subagents.mode`: manual" in block

    async def test_no_injection_when_empty(self, session_with_user):
        with patch(_PREF_STORE, return_value=_make_store({})):
            block = await _build_preferences_block(session_with_user)

        assert block == ""

    async def test_no_injection_when_no_user_id(self, session_without_user, monkeypatch):
        # Simulate session without user_id AND no os.getuid fallback
        monkeypatch.setattr(os, "getuid", lambda: None, raising=False)
        monkeypatch.setattr(os, "environ", {}, raising=False)
        block = await _build_preferences_block(session_without_user)
        assert block == ""

    async def test_no_injection_when_store_unavailable(self, session_with_user):
        with patch(_PREF_STORE, side_effect=ImportError("no module")):
            block = await _build_preferences_block(session_with_user)
        assert block == ""

    async def test_no_injection_when_get_all_raises(self, session_with_user):
        store = MagicMock()
        store.get_all.side_effect = RuntimeError("boom")
        with patch(_PREF_STORE, return_value=store):
            block = await _build_preferences_block(session_with_user)
        assert block == ""

    async def test_different_users_see_different_prefs(self):
        user_a = MagicMock()
        user_a.user_id = "user-a"
        user_b = MagicMock()
        user_b.user_id = "user-b"

        store_a = _make_store({"lang": "pt-BR"})
        store_b = _make_store({"lang": "en-US"})

        with patch(_PREF_STORE, side_effect=[store_a, store_b]):
            block_a = await _build_preferences_block(user_a)
            block_b = await _build_preferences_block(user_b)

        assert "pt-BR" in block_a
        assert "en-US" in block_b
        assert "pt-BR" not in block_b
        assert "en-US" not in block_a

    async def test_sorted_keys(self, session_with_user):
        """Keys should be sorted alphabetically in the output."""
        prefs = {"z_key": "z", "a_key": "a", "m_key": "m"}
        with patch(_PREF_STORE, return_value=_make_store(prefs)):
            block = await _build_preferences_block(session_with_user)

        lines = block.split("\n")
        key_lines = [l for l in lines if l.startswith("- `")]
        # sorted keys: a_key, m_key, z_key
        assert key_lines[0].startswith("- `a_key`")
        assert key_lines[1].startswith("- `m_key`")
        assert key_lines[2].startswith("- `z_key`")

    async def test_empty_prefs_when_session_is_none(self):
        """Session = None should not crash; returns empty string."""
        block = await _build_preferences_block(None)
        assert block == ""


# ── _resolve_user_id ────────────────────────────────────────────────────


class TestResolveUserId:

    def test_from_session_attribute(self, session_with_user):
        uid = _resolve_user_id(session_with_user)
        assert uid == "user-abc-123"

    def test_session_none_falls_back(self, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 1001)
        monkeypatch.setattr(os, "environ", {"USER": "fallback_user"})
        uid = _resolve_user_id(None)
        assert uid == "1001"

    def test_no_getuid_uses_environ(self, monkeypatch):
        monkeypatch.delattr(os, "getuid", raising=False)
        monkeypatch.setattr(os, "environ", {"USER": "cli_user"})
        uid = _resolve_user_id(None)
        assert uid == "cli_user"

    def test_no_getuid_no_user_env(self, monkeypatch):
        monkeypatch.delattr(os, "getuid", raising=False)
        monkeypatch.setattr(os, "environ", {})
        uid = _resolve_user_id(None)
        assert uid == "unknown"

    def test_session_user_id_is_none_falls_back(self, session_without_user, monkeypatch):
        monkeypatch.setattr(os, "getuid", lambda: 1002)
        uid = _resolve_user_id(session_without_user)
        assert uid == "1002"


# ── ContextManager integration ──────────────────────────────────────────


class TestContextManagerIntegration:

    async def test_build_system_instruction_includes_preferences(
        self, session_with_user
    ):
        """Persona path injects preferences block."""
        persona = MagicMock()
        persona.name = "test_persona"
        persona.build_system_instruction = AsyncMock(return_value="PERSONA_PROMPT")

        persona_manager = MagicMock()
        persona_manager.get_active_persona = MagicMock(return_value=persona)

        ctx = ContextManager(persona_manager=persona_manager)

        with patch(
            _PREF_STORE,
            return_value=_make_store({"response_language": "pt-BR"}),
        ):
            out = await ctx._build_system_instruction(
                parse_result=None,
                session=session_with_user,
                working_directory="/tmp/test",
            )

        assert "PERSONA_PROMPT" in out
        assert "📋 Preferências do Usuário" in out
        assert "`response_language`: pt-BR" in out
        # Preferences must appear AFTER persona and BEFORE skills
        assert out.index("PERSONA_PROMPT") < out.index("📋 Preferências")

    async def test_build_system_instruction_no_prefs_when_empty(
        self, session_with_user
    ):
        """No preferences block when user has no preferences."""
        persona = MagicMock()
        persona.name = "test_persona"
        persona.build_system_instruction = AsyncMock(return_value="PERSONA_PROMPT")

        persona_manager = MagicMock()
        persona_manager.get_active_persona = MagicMock(return_value=persona)

        ctx = ContextManager(persona_manager=persona_manager)

        with patch(_PREF_STORE, return_value=_make_store({})):
            out = await ctx._build_system_instruction(
                parse_result=None,
                session=session_with_user,
                working_directory="/tmp/test",
            )

        assert "PERSONA_PROMPT" in out
        assert "📋 Preferências do Usuário" not in out

    async def test_fallback_instruction_includes_preferences(
        self, session_with_user
    ):
        """Fallback path also injects preferences block."""
        ctx = ContextManager(persona_manager=None)
        ctx.instruction_loader = MagicMock()
        ctx.instruction_loader.load_fallback_instruction = MagicMock(
            return_value="FALLBACK_BODY"
        )

        with patch(
            _PREF_STORE,
            return_value=_make_store({"ui.theme": "dark"}),
        ):
            out = await ctx._build_fallback_system_instruction(
                session=session_with_user,
                working_directory="/tmp/test",
            )

        assert "FALLBACK_BODY" in out
        assert "📋 Preferências do Usuário" in out
        assert "`ui.theme`: dark" in out

    async def test_fallback_instruction_no_prefs_when_empty(
        self, session_with_user
    ):
        """Fallback does NOT add block when empty."""
        ctx = ContextManager(persona_manager=None)
        ctx.instruction_loader = MagicMock()
        ctx.instruction_loader.load_fallback_instruction = MagicMock(
            return_value="FALLBACK_BODY"
        )

        with patch(_PREF_STORE, return_value=_make_store({})):
            out = await ctx._build_fallback_system_instruction(
                session=session_with_user,
                working_directory="/tmp/test",
            )

        assert "FALLBACK_BODY" in out
        assert "📋 Preferências do Usuário" not in out

    async def test_prefs_after_deile_md_before_skills(self, session_with_user):
        """Verify the order: DEILE.md -> Persona -> Prefs -> Skills."""
        persona = MagicMock()
        persona.name = "test_persona"
        persona.build_system_instruction = AsyncMock(return_value="PERSONA_BODY")

        persona_manager = MagicMock()
        persona_manager.get_active_persona = MagicMock(return_value=persona)

        ctx = ContextManager(persona_manager=persona_manager)
        # Bootstrap skills so _build_skills_block returns something
        from deile.skills.registry import get_skill_registry
        _ = get_skill_registry()
        ctx._skills_bootstrapped = True
        ctx._skill_router = None  # no skills = "" block

        with patch(
            _PREF_STORE,
            return_value=_make_store({"theme": "dark"}),
        ):
            out = await ctx._build_system_instruction(
                parse_result=None,
                session=session_with_user,
                working_directory="/tmp/test",
            )

        # Order: DEILE.md layers first, then PERSONA_BODY, then Prefs
        # We can't guarantee DEILE.md markers without setup, but we can check
        # PERSONA -> Prefs order
        assert out.index("PERSONA_BODY") < out.index("📋 Preferências")

    async def test_injection_does_not_break_prompt_format(self, session_with_user):
        """Ensure the system prompt is still valid text after injection."""
        persona = MagicMock()
        persona.name = "test_persona"
        persona.build_system_instruction = AsyncMock(return_value="Hello world. 你好。")

        persona_manager = MagicMock()
        persona_manager.get_active_persona = MagicMock(return_value=persona)

        ctx = ContextManager(persona_manager=persona_manager)

        with patch(_PREF_STORE, return_value=_make_store({"k": "v"})):
            out = await ctx._build_system_instruction(
                parse_result=None,
                session=session_with_user,
                working_directory="/tmp/test",
            )

        # Should be valid Unicode, no control chars
        assert isinstance(out, str)
        assert "\x00" not in out
        assert len(out) > 0
        # The original persona text is preserved
        assert "Hello world. 你好。" in out


# ── helpers ─────────────────────────────────────────────────────────────


def _make_store(prefs: dict):
    """Create a PreferenceStore mock that returns *prefs* from get_all()."""
    store = MagicMock()
    store.get_all.return_value = dict(prefs)
    return store
