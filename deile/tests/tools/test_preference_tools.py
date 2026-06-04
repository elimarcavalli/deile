"""Unit tests for ``deile.tools.preference_tools``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from deile.tools.base import ToolContext
from deile.tools.preference_tools import (ForgetPreferenceTool,
                                          ListPreferencesTool,
                                          RememberPreferenceTool,
                                          _coerce_value, _resolve_user_id)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_prefs_file(tmp_path: Path):
    """Redirect the PreferenceStore backing file to a temp path."""
    prefs_file = tmp_path / "preferences.json"
    with patch(
        "deile.preferences.store._PREFS_FILE", prefs_file
    ), patch(
        "deile.preferences.store._PREFS_DIR", tmp_path
    ), patch(
        "deile.tools.preference_tools.PreferenceStore",
    ) as mock_store_cls:
        # We use the real store but with patched file paths.
        # So let the PreferenceStore class resolve normally.
        pass


def _ctx(user_id: str = "test_user", **parsed_args) -> ToolContext:
    return ToolContext(
        user_input="",
        parsed_args=parsed_args,
        session_data={"user_id": user_id},
    )


def _bypass_permission():
    """Make _check_write_permission always return True."""
    return patch(
        "deile.tools.preference_tools._check_write_permission",
        return_value=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────


class TestResolveUserId:
    def test_from_session_data(self):
        ctx = ToolContext(
            user_input="",
            parsed_args={},
            session_data={"user_id": "alice"},
        )
        assert _resolve_user_id(ctx) == "alice"

    def test_from_metadata(self):
        ctx = ToolContext(
            user_input="",
            parsed_args={},
            metadata={"user_id": "bob"},
        )
        assert _resolve_user_id(ctx) == "bob"

    def test_fallback_unknown(self):
        ctx = ToolContext(user_input="", parsed_args={})
        assert _resolve_user_id(ctx) == "unknown"


class TestCoerceValue:
    def test_true_false(self):
        assert _coerce_value("true") is True
        assert _coerce_value("false") is False

    def test_integers(self):
        assert _coerce_value("42") == 42
        assert _coerce_value("-7") == -7

    def test_floats(self):
        assert _coerce_value("3.14") == 3.14

    def test_null_none(self):
        assert _coerce_value("null") is None
        assert _coerce_value("none") is None

    def test_string_passthrough(self):
        assert _coerce_value("hello world") == "hello world"


# ── remember_preference ──────────────────────────────────────────────────


class TestRememberPreference:

    @pytest.mark.asyncio
    async def test_success(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="theme", value="dark")
            result = await tool.execute(ctx)

        assert result.is_success
        assert "stored successfully" in result.message

    @pytest.mark.asyncio
    async def test_invalid_key_rejected(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="BadKey!", value="x")
            result = await tool.execute(ctx)

        assert result.is_error
        assert "Invalid preference key" in result.message

    @pytest.mark.asyncio
    async def test_value_too_long_rejected(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="x", value="y" * 4097)
            result = await tool.execute(ctx)

        assert result.is_error
        assert "maximum length" in result.message

    @pytest.mark.asyncio
    async def test_missing_key(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ):
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", value="x")
            result = await tool.execute(ctx)

        assert result.is_error
        assert "key" in result.message.lower()

    @pytest.mark.asyncio
    async def test_missing_value(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ):
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="x")
            result = await tool.execute(ctx)

        assert result.is_error
        assert "value" in result.message.lower()

    @pytest.mark.asyncio
    async def test_permission_denied(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), patch(
            "deile.tools.preference_tools._check_write_permission",
            return_value=False,
        ):
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="theme", value="dark")
            result = await tool.execute(ctx)

        assert result.is_error
        assert "Permission denied" in result.message

    @pytest.mark.asyncio
    async def test_boolean_coerced(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="auto_mode", value="true")
            result = await tool.execute(ctx)

        assert result.is_success
        assert result.data["value"] is True

    @pytest.mark.asyncio
    async def test_dot_namespaced_key(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            tool = RememberPreferenceTool()
            ctx = _ctx(user_id="u1", key="subagents.mode", value="manual")
            result = await tool.execute(ctx)

        assert result.is_success


# ── list_preferences ──────────────────────────────────────────────────────


class TestListPreferences:

    @pytest.mark.asyncio
    async def test_empty(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ):
            tool = ListPreferencesTool()
            ctx = _ctx(user_id="u1")
            result = await tool.execute(ctx)

        assert result.is_success
        assert result.data["preferences"] == {}

    @pytest.mark.asyncio
    async def test_list_all(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            store_tool = RememberPreferenceTool()
            await store_tool.execute(_ctx(user_id="u1", key="theme", value="dark"))
            await store_tool.execute(
                _ctx(user_id="u1", key="lang", value="pt")
            )

            list_tool = ListPreferencesTool()
            result = await list_tool.execute(_ctx(user_id="u1"))

        assert result.is_success
        prefs = result.data["preferences"]
        assert prefs["theme"] == "dark"
        assert prefs["lang"] == "pt"

    @pytest.mark.asyncio
    async def test_list_with_prefix(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            store_tool = RememberPreferenceTool()
            await store_tool.execute(
                _ctx(user_id="u1", key="subagents.mode", value="auto")
            )
            await store_tool.execute(
                _ctx(user_id="u1", key="subagents.count", value="3")
            )
            await store_tool.execute(
                _ctx(user_id="u1", key="ui.theme", value="light")
            )

            list_tool = ListPreferencesTool()
            result = await list_tool.execute(
                _ctx(user_id="u1", prefix="subagents")
            )

        assert result.is_success
        prefs = result.data["preferences"]
        assert "subagents.mode" in prefs
        assert "subagents.count" in prefs
        assert "ui.theme" not in prefs


# ── forget_preference ─────────────────────────────────────────────────────


class TestForgetPreference:

    @pytest.mark.asyncio
    async def test_forget_existing(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            store_tool = RememberPreferenceTool()
            await store_tool.execute(_ctx(user_id="u1", key="theme", value="dark"))

            forget_tool = ForgetPreferenceTool()
            result = await forget_tool.execute(_ctx(user_id="u1", key="theme"))

        assert result.is_success
        assert result.data["deleted"] is True

    @pytest.mark.asyncio
    async def test_forget_nonexistent_idempotent(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), _bypass_permission():
            tool = ForgetPreferenceTool()
            result = await tool.execute(_ctx(user_id="u1", key="ghost"))

        assert result.is_success
        assert result.data["deleted"] is False
        assert "did not exist" in result.message.lower()

    @pytest.mark.asyncio
    async def test_missing_key(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ):
            tool = ForgetPreferenceTool()
            ctx = _ctx(user_id="u1")
            result = await tool.execute(ctx)

        assert result.is_error
        assert "key" in result.message.lower()

    @pytest.mark.asyncio
    async def test_permission_denied(self, tmp_path: Path):
        prefs_file = tmp_path / "preferences.json"
        with patch(
            "deile.preferences.store._PREFS_FILE", prefs_file
        ), patch(
            "deile.preferences.store._PREFS_DIR", tmp_path
        ), patch(
            "deile.tools.preference_tools._check_write_permission",
            return_value=False,
        ):
            tool = ForgetPreferenceTool()
            ctx = _ctx(user_id="u1", key="theme")
            result = await tool.execute(ctx)

        assert result.is_error
        assert "Permission denied" in result.message


# ── Tool registration ─────────────────────────────────────────────────────


def test_tools_discoverable():
    """All three tools should be auto-discovered via DEFAULT_TOOL_PACKAGES."""
    from deile.tools.discovery import (DEFAULT_TOOL_PACKAGES,
                                       discover_tools_in_package)
    from deile.tools.registry import ToolRegistry

    assert "deile.tools.preference_tools" in DEFAULT_TOOL_PACKAGES

    registry = ToolRegistry()
    count = discover_tools_in_package(registry, "deile.tools.preference_tools")
    assert count == 3

    assert "remember_preference" in registry
    assert "list_preferences" in registry
    assert "forget_preference" in registry

    # Verify category
    from deile.tools.base import ToolCategory

    assert registry.get("remember_preference").category == ToolCategory.SYSTEM.value
    assert registry.get("list_preferences").category == ToolCategory.SYSTEM.value
    assert registry.get("forget_preference").category == ToolCategory.SYSTEM.value
