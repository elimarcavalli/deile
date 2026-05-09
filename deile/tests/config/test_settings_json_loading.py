"""Tests for Settings JSON loading: hierarchy, env-var fallback, and deprecation.

Covers the new _build_settings() / _load_json_file() / _apply_nested_dict()
/ _apply_env_overrides() logic introduced in issue #111.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from deile.config.settings import (Settings, _apply_env_overrides,
                                   _apply_nested_dict, _load_json_file,
                                   reset_settings)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# _load_json_file
# ---------------------------------------------------------------------------


class TestLoadJsonFile:
    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_json_file(tmp_path / "missing.json") == {}

    def test_valid_dict_returned(self, tmp_path):
        p = tmp_path / "s.json"
        _write_json(p, {"debug": {"enabled": True}})
        assert _load_json_file(p) == {"debug": {"enabled": True}}

    def test_invalid_json_returns_empty(self, tmp_path, caplog):
        p = tmp_path / "bad.json"
        p.write_text("not-json!", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            result = _load_json_file(p)
        assert result == {}
        assert "Cannot read" in caplog.text

    def test_non_dict_json_returns_empty(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert _load_json_file(p) == {}


# ---------------------------------------------------------------------------
# _apply_nested_dict — JSON → Settings field mapping
# ---------------------------------------------------------------------------


class TestApplyNestedDict:
    def test_debug_enabled(self):
        s = Settings()
        _apply_nested_dict(s, {"debug": {"enabled": True}})
        assert s.debug_enabled is True

    def test_loop_guard_max_calls(self):
        s = Settings()
        _apply_nested_dict(s, {"loop_guard": {"max_calls": 99}})
        assert s.loop_guard_max_calls == 99

    def test_loop_guard_disabled(self):
        s = Settings()
        _apply_nested_dict(s, {"loop_guard": {"disabled": True}})
        assert s.loop_guard_disabled is True

    def test_pipeline_repo(self):
        s = Settings()
        _apply_nested_dict(s, {"pipeline": {"repo": "myorg/myrepo"}})
        assert s.pipeline_repo == "myorg/myrepo"

    def test_pipeline_poll_interval(self):
        s = Settings()
        _apply_nested_dict(s, {"pipeline": {"poll_interval": 120}})
        assert s.pipeline_poll_interval == 120

    def test_cron_poll_interval(self):
        s = Settings()
        _apply_nested_dict(s, {"cron": {"poll_interval": 15}})
        assert s.cron_poll_interval == 15

    def test_vision_model(self):
        s = Settings()
        _apply_nested_dict(s, {"model": {"vision_model": "my-vision-model"}})
        assert s.vision_model == "my-vision-model"

    def test_approval_auto(self):
        s = Settings()
        _apply_nested_dict(s, {"approval": {"auto": True}})
        assert s.bot_approval_auto is True

    def test_pipeline_base_path_converted_to_path(self, tmp_path):
        s = Settings()
        _apply_nested_dict(s, {"pipeline": {"base_path": str(tmp_path)}})
        assert isinstance(s.pipeline_base_path, Path)
        assert s.pipeline_base_path == tmp_path

    def test_unknown_key_ignored(self):
        s = Settings()
        _apply_nested_dict(s, {"nonexistent_key": {"foo": "bar"}})
        assert not hasattr(s, "nonexistent_key")

    def test_bool_coercion_from_string(self):
        s = Settings()
        _apply_nested_dict(s, {"debug": {"enabled": "true"}})
        assert s.debug_enabled is True

    def test_int_coercion_from_string(self):
        s = Settings()
        _apply_nested_dict(s, {"loop_guard": {"max_calls": "42"}})
        assert s.loop_guard_max_calls == 42


# ---------------------------------------------------------------------------
# _apply_env_overrides
# ---------------------------------------------------------------------------


class TestApplyEnvOverrides:
    def test_deile_debug_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_DEBUG", "1")
        s = Settings()
        _apply_env_overrides(s)
        assert s.debug_enabled is True

    def test_deile_preferred_model_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_PREFERRED_MODEL", "anthropic:claude-opus-4-7")
        s = Settings()
        _apply_env_overrides(s)
        assert s.preferred_model == "anthropic:claude-opus-4-7"

    def test_deile_vision_model_applies(self, monkeypatch):
        monkeypatch.setenv("DEILE_VISION_MODEL", "custom-vision-v1")
        s = Settings()
        _apply_env_overrides(s)
        assert s.vision_model == "custom-vision-v1"

    def test_deile_bot_approval_auto_truthy(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_APPROVAL_AUTO", "true")
        s = Settings()
        _apply_env_overrides(s)
        assert s.bot_approval_auto is True

    def test_deile_bot_approval_auto_falsy(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_APPROVAL_AUTO", "0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.bot_approval_auto is False

    def test_deile_loop_guard_disable(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOOP_GUARD_DISABLE", "1")
        s = Settings()
        _apply_env_overrides(s)
        assert s.loop_guard_disabled is True

    def test_deile_loop_guard_max_calls(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOOP_GUARD_MAX_CALLS", "25")
        s = Settings()
        _apply_env_overrides(s)
        assert s.loop_guard_max_calls == 25

    def test_deile_pipeline_repo(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_REPO", "myorg/myrepo")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_repo == "myorg/myrepo"

    def test_deile_pipeline_poll_interval(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_POLL_INTERVAL", "120")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_poll_interval == 120

    def test_deile_cron_poll_interval(self, monkeypatch):
        monkeypatch.setenv("DEILE_CRON_POLL_INTERVAL", "15")
        s = Settings()
        _apply_env_overrides(s)
        assert s.cron_poll_interval == 15

    def test_env_override_emits_deprecation_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("DEILE_DEBUG", "1")
        s = Settings()
        with caplog.at_level(logging.WARNING):
            _apply_env_overrides(s)
        assert "deprecated" in caplog.text.lower()
        assert "DEILE_DEBUG" in caplog.text

    def test_absent_env_var_leaves_default(self, monkeypatch):
        monkeypatch.delenv("DEILE_VISION_MODEL", raising=False)
        s = Settings()
        _apply_env_overrides(s)
        assert s.vision_model == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# _build_settings — hierarchy integration
# ---------------------------------------------------------------------------


class TestBuildSettings:
    def test_default_values_when_no_files(self, monkeypatch, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir(exist_ok=True)
        monkeypatch.setattr("deile.config.settings.Path.home", lambda: tmp_path / "home")
        monkeypatch.chdir(proj)
        reset_settings()
        from deile.config.settings import get_settings

        s = get_settings()
        assert s.pipeline_repo == "elimarcavalli/deile"
        assert s.cron_poll_interval == 30
        assert s.debug_enabled is False
        reset_settings()

    def test_global_json_applied(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        proj = tmp_path / "project"
        proj.mkdir(parents=True)
        global_settings = home / ".deile" / "settings.json"
        _write_json(global_settings, {"pipeline": {"poll_interval": 90}})
        monkeypatch.setattr("deile.config.settings.Path.home", lambda: home)
        monkeypatch.chdir(proj)
        reset_settings()
        from deile.config.settings import get_settings

        s = get_settings()
        assert s.pipeline_poll_interval == 90
        reset_settings()

    def test_project_json_overrides_global(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        proj = tmp_path / "project"
        proj.mkdir(parents=True)
        _write_json(home / ".deile" / "settings.json", {"cron": {"poll_interval": 60}})
        _write_json(proj / ".deile" / "settings.json", {"cron": {"poll_interval": 10}})
        monkeypatch.setattr("deile.config.settings.Path.home", lambda: home)
        monkeypatch.chdir(proj)
        reset_settings()
        from deile.config.settings import get_settings

        s = get_settings()
        assert s.cron_poll_interval == 10
        reset_settings()

    def test_env_var_overrides_json(self, monkeypatch, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir(parents=True)
        _write_json(
            proj / ".deile" / "settings.json",
            {"pipeline": {"repo": "from-json/repo"}},
        )
        monkeypatch.setattr("deile.config.settings.Path.home", lambda: tmp_path / "home")
        monkeypatch.chdir(proj)
        monkeypatch.setenv("DEILE_PIPELINE_REPO", "from-env/repo")
        reset_settings()
        from deile.config.settings import get_settings

        s = get_settings()
        assert s.pipeline_repo == "from-env/repo"
        reset_settings()

    def test_reset_clears_singleton(self, tmp_path, monkeypatch):
        monkeypatch.setattr("deile.config.settings.Path.home", lambda: tmp_path / "home")
        monkeypatch.chdir(tmp_path)
        reset_settings()
        from deile.config.settings import get_settings

        s1 = get_settings()
        reset_settings()
        s2 = get_settings()
        assert s1 is not s2


# ---------------------------------------------------------------------------
# SettingsManager.get_all_preferences
# ---------------------------------------------------------------------------


class TestSettingsManagerGetAllPreferences:
    def test_empty_when_no_files(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        mgr = SettingsManager(project_dir=tmp_path / "project", user_home=tmp_path / "home")
        assert mgr.get_all_preferences() == {}

    def test_global_prefs_returned(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        mgr = SettingsManager(project_dir=tmp_path / "project", user_home=tmp_path / "home")
        _write_json(
            mgr.global_settings_path,
            {"loop_guard": {"max_calls": 77}, "skills_paths": []},
        )
        prefs = mgr.get_all_preferences()
        assert prefs["loop_guard"]["max_calls"] == 77

    def test_project_overrides_global(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        mgr = SettingsManager(project_dir=tmp_path / "project", user_home=tmp_path / "home")
        _write_json(
            mgr.global_settings_path,
            {"pipeline": {"repo": "global/repo"}, "skills_paths": []},
        )
        _write_json(
            mgr.project_settings_path,
            {"pipeline": {"repo": "project/repo"}, "skills_paths": []},
        )
        prefs = mgr.get_all_preferences()
        assert prefs["pipeline"]["repo"] == "project/repo"

    def test_set_preference_persists(self, tmp_path, allow_settings_writes):
        # ``set_preference`` is fail-closed by default (issue #125 P0-2 / P1-5);
        # the ``allow_settings_writes`` fixture (root conftest) installs a
        # permissive override and restores the saved rule on teardown so the
        # mutation never leaks into neighboring test files.
        from deile.commands.settings_manager import SettingsManager

        mgr = SettingsManager(project_dir=tmp_path / "project", user_home=tmp_path / "home")
        assert mgr.set_preference("debug", {"enabled": True}) is True
        prefs = mgr.get_all_preferences()
        assert prefs["debug"]["enabled"] is True

    def test_load_all_preferences_invalid_scope(self, tmp_path):
        from deile.commands.settings_manager import SettingsManager

        mgr = SettingsManager(project_dir=tmp_path / "project", user_home=tmp_path / "home")
        with pytest.raises(ValueError, match="Invalid scope"):
            mgr.load_all_preferences("invalid")
