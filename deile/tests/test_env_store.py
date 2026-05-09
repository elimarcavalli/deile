"""Tests for deile.config.env_store."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest


def _write_settings(home: Path, data: dict) -> None:
    d = home / ".deile"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def _read_settings(home: Path) -> dict:
    p = home / ".deile" / "settings.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
@pytest.mark.unit
class TestIsSensitive:
    def test_api_key(self):
        from deile.config.env_store import is_sensitive
        assert is_sensitive("ANTHROPIC_API_KEY")

    def test_token(self):
        from deile.config.env_store import is_sensitive
        assert is_sensitive("GITHUB_TOKEN")

    def test_secret(self):
        from deile.config.env_store import is_sensitive
        assert is_sensitive("DATABASE_SECRET")

    def test_password(self):
        from deile.config.env_store import is_sensitive
        assert is_sensitive("DB_PASSWORD")

    def test_non_sensitive(self):
        from deile.config.env_store import is_sensitive
        assert not is_sensitive("MY_CUSTOM_VAR")

    def test_case_insensitive(self):
        from deile.config.env_store import is_sensitive
        assert is_sensitive("my_api_key")

@pytest.mark.unit
class TestLoadExportedVars:
    def test_missing_file_is_noop(self, tmp_path, monkeypatch):
        from deile.config.env_store import load_exported_vars
        monkeypatch.delenv("DEILE_TEST_VAR_LOAD", raising=False)
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {}

    def test_loads_into_os_environ(self, tmp_path, monkeypatch):
        from deile.config.env_store import load_exported_vars
        monkeypatch.delenv("DEILE_TEST_VAR_LOAD", raising=False)
        _write_settings(tmp_path, {"env": {"exports": {"DEILE_TEST_VAR_LOAD": "hello"}}})
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {"DEILE_TEST_VAR_LOAD": "hello"}
        assert os.environ["DEILE_TEST_VAR_LOAD"] == "hello"

    def test_existing_env_not_overridden(self, tmp_path, monkeypatch):
        from deile.config.env_store import load_exported_vars
        monkeypatch.setenv("DEILE_TEST_VAR_SKIP", "existing")
        _write_settings(tmp_path, {"env": {"exports": {"DEILE_TEST_VAR_SKIP": "new_value"}}})
        loaded = load_exported_vars(home=tmp_path)
        assert "DEILE_TEST_VAR_SKIP" not in loaded
        assert os.environ["DEILE_TEST_VAR_SKIP"] == "existing"

    def test_empty_exports_section(self, tmp_path):
        from deile.config.env_store import load_exported_vars
        _write_settings(tmp_path, {"env": {"exports": {}}})
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {}

    def test_non_dict_exports_ignored(self, tmp_path):
        from deile.config.env_store import load_exported_vars
        _write_settings(tmp_path, {"env": {"exports": ["not", "a", "dict"]}})
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {}

    def test_malformed_json_is_noop(self, tmp_path):
        from deile.config.env_store import load_exported_vars
        d = tmp_path / ".deile"
        d.mkdir(parents=True, exist_ok=True)
        (d / "settings.json").write_text("{broken json", encoding="utf-8")
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {}

    def test_non_string_value_coerced(self, tmp_path, monkeypatch):
        from deile.config.env_store import load_exported_vars
        monkeypatch.delenv("DEILE_INT_VAR", raising=False)
        _write_settings(tmp_path, {"env": {"exports": {"DEILE_INT_VAR": 42}}})
        loaded = load_exported_vars(home=tmp_path)
        assert loaded["DEILE_INT_VAR"] == "42"
        assert os.environ["DEILE_INT_VAR"] == "42"

    def test_empty_key_skipped(self, tmp_path):
        from deile.config.env_store import load_exported_vars
        _write_settings(tmp_path, {"env": {"exports": {"": "value"}}})
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {}

    def test_multiple_vars_loaded(self, tmp_path, monkeypatch):
        from deile.config.env_store import load_exported_vars
        monkeypatch.delenv("DEILE_A", raising=False)
        monkeypatch.delenv("DEILE_B", raising=False)
        _write_settings(tmp_path, {"env": {"exports": {"DEILE_A": "1", "DEILE_B": "2"}}})
        loaded = load_exported_vars(home=tmp_path)
        assert set(loaded) == {"DEILE_A", "DEILE_B"}

    def test_other_settings_preserved_on_load(self, tmp_path, monkeypatch):
        from deile.config.env_store import load_exported_vars
        monkeypatch.delenv("DEILE_X", raising=False)
        _write_settings(tmp_path, {
            "debug": True,
            "env": {"exports": {"DEILE_X": "x"}},
        })
        loaded = load_exported_vars(home=tmp_path)
        assert loaded == {"DEILE_X": "x"}

@pytest.mark.unit
class TestStoreVar:
    def test_creates_settings_file(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        monkeypatch.delenv("MY_VAR", raising=False)
        ok = store_var("MY_VAR", "hello", home=tmp_path)
        assert ok is True
        data = _read_settings(tmp_path)
        assert data["env"]["exports"]["MY_VAR"] == "hello"

    def test_sets_os_environ(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        monkeypatch.delenv("MY_VAR2", raising=False)
        store_var("MY_VAR2", "world", home=tmp_path)
        assert os.environ["MY_VAR2"] == "world"

    def test_overwrite_existing(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        _write_settings(tmp_path, {"env": {"exports": {"MY_VAR3": "old"}}})
        monkeypatch.setenv("MY_VAR3", "old")
        store_var("MY_VAR3", "new", home=tmp_path)
        data = _read_settings(tmp_path)
        assert data["env"]["exports"]["MY_VAR3"] == "new"

    def test_preserves_other_exports(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        _write_settings(tmp_path, {"env": {"exports": {"OTHER": "kept"}}})
        monkeypatch.delenv("NEW_VAR", raising=False)
        store_var("NEW_VAR", "added", home=tmp_path)
        data = _read_settings(tmp_path)
        assert data["env"]["exports"]["OTHER"] == "kept"
        assert data["env"]["exports"]["NEW_VAR"] == "added"

    def test_preserves_other_top_level_settings(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        _write_settings(tmp_path, {"debug": True, "env": {"exports": {}}})
        monkeypatch.delenv("TOPVAR", raising=False)
        store_var("TOPVAR", "v", home=tmp_path)
        data = _read_settings(tmp_path)
        assert data["debug"] is True

    def test_empty_key_raises(self, tmp_path):
        from deile.config.env_store import store_var
        with pytest.raises(ValueError):
            store_var("", "value", home=tmp_path)

    def test_whitespace_only_key_raises(self, tmp_path):
        from deile.config.env_store import store_var
        with pytest.raises(ValueError):
            store_var("   ", "value", home=tmp_path)

    def test_invalid_char_in_key_raises(self, tmp_path):
        from deile.config.env_store import store_var
        with pytest.raises(ValueError, match="Invalid key"):
            store_var("MY-VAR", "value", home=tmp_path)

    def test_non_string_value_raises(self, tmp_path):
        from deile.config.env_store import store_var
        with pytest.raises(TypeError):
            store_var("MY_VAR4", 42, home=tmp_path)

    def test_file_permissions_600(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        monkeypatch.delenv("PERM_VAR", raising=False)
        store_var("PERM_VAR", "v", home=tmp_path)
        path = tmp_path / ".deile" / "settings.json"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_key_with_underscores(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        monkeypatch.delenv("MY_VALID_KEY_123", raising=False)
        ok = store_var("MY_VALID_KEY_123", "ok", home=tmp_path)
        assert ok is True

    def test_spaces_stripped_from_key(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var
        monkeypatch.delenv("STRIPPED", raising=False)
        ok = store_var("  STRIPPED  ", "v", home=tmp_path)
        assert ok is True
        data = _read_settings(tmp_path)
        assert "STRIPPED" in data["env"]["exports"]

@pytest.mark.unit
class TestUnsetVar:
    def test_returns_true_when_existed(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var, unset_var
        monkeypatch.delenv("RM_VAR", raising=False)
        store_var("RM_VAR", "v", home=tmp_path)
        result = unset_var("RM_VAR", home=tmp_path)
        assert result is True

    def test_returns_false_when_not_found(self, tmp_path):
        from deile.config.env_store import unset_var
        result = unset_var("DOES_NOT_EXIST", home=tmp_path)
        assert result is False

    def test_removes_from_settings(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var, unset_var
        monkeypatch.delenv("DEL_VAR", raising=False)
        store_var("DEL_VAR", "v", home=tmp_path)
        unset_var("DEL_VAR", home=tmp_path)
        data = _read_settings(tmp_path)
        assert "DEL_VAR" not in data.get("env", {}).get("exports", {})

    def test_removes_from_os_environ(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var, unset_var
        monkeypatch.delenv("ENV_DEL", raising=False)
        store_var("ENV_DEL", "v", home=tmp_path)
        assert "ENV_DEL" in os.environ
        unset_var("ENV_DEL", home=tmp_path)
        assert "ENV_DEL" not in os.environ

    def test_preserves_other_exports(self, tmp_path, monkeypatch):
        from deile.config.env_store import store_var, unset_var
        monkeypatch.delenv("K1", raising=False)
        monkeypatch.delenv("K2", raising=False)
        store_var("K1", "v1", home=tmp_path)
        store_var("K2", "v2", home=tmp_path)
        unset_var("K1", home=tmp_path)
        data = _read_settings(tmp_path)
        assert "K1" not in data["env"]["exports"]
        assert data["env"]["exports"]["K2"] == "v2"

    def test_empty_key_raises(self, tmp_path):
        from deile.config.env_store import unset_var
        with pytest.raises(ValueError):
            unset_var("", home=tmp_path)

    def test_missing_file_returns_false(self, tmp_path):
        from deile.config.env_store import unset_var
        result = unset_var("GHOST", home=tmp_path)
        assert result is False


@pytest.mark.unit
class TestListVars:
    def test_empty_when_no_file(self, tmp_path):
        from deile.config.env_store import list_vars
        assert list_vars(home=tmp_path) == {}

    def test_sensitive_keys_masked(self, tmp_path, monkeypatch):
        from deile.config.env_store import list_vars, store_var
        monkeypatch.delenv("MY_API_KEY", raising=False)
        store_var("MY_API_KEY", "sk-secret", home=tmp_path)
        result = list_vars(home=tmp_path)
        assert result["MY_API_KEY"] == "<masked>"

    def test_non_sensitive_value_shown(self, tmp_path, monkeypatch):
        from deile.config.env_store import list_vars, store_var
        monkeypatch.delenv("MY_CUSTOM_VAR", raising=False)
        store_var("MY_CUSTOM_VAR", "hello", home=tmp_path)
        result = list_vars(home=tmp_path)
        assert result["MY_CUSTOM_VAR"] == "hello"

    def test_multiple_vars(self, tmp_path, monkeypatch):
        from deile.config.env_store import list_vars, store_var
        monkeypatch.delenv("A_KEY", raising=False)
        monkeypatch.delenv("B_VAR", raising=False)
        store_var("A_KEY", "secret", home=tmp_path)
        store_var("B_VAR", "public", home=tmp_path)
        result = list_vars(home=tmp_path)
        assert result["A_KEY"] == "<masked>"
        assert result["B_VAR"] == "public"

    def test_non_string_key_skipped(self, tmp_path):
        from deile.config.env_store import list_vars
        _write_settings(tmp_path, {"env": {"exports": {"VALID": "ok"}}})
        result = list_vars(home=tmp_path)
        assert "VALID" in result
