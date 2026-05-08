"""Tests: layered ``.deile/settings.json`` flow (issue #111).

Covers:
  - ``Settings.apply_overrides()`` mapping nested JSON keys to flat fields
  - ``get_settings()`` reading project > user > defaults via SettingsManager
  - Legacy ``config/settings.json`` fallback when no new-layer file exists
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from deile.config.settings import (LogLevel, Settings, get_settings,
                                   reset_settings)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_settings()
    # The CLI globally disables logging on boot (see 09-CONFIGURACAO.md);
    # other tests in the suite trip that toggle and starve caplog. Restore
    # default behavior so warning assertions in this module are reliable.
    logging.disable(logging.NOTSET)
    yield
    reset_settings()


# ---------------------------------------------------------------------------
# apply_overrides
# ---------------------------------------------------------------------------


class TestApplyOverrides:
    def test_no_op_on_empty_dict(self):
        s = Settings()
        original_log_level = s.log_level
        s.apply_overrides({})
        assert s.log_level == original_log_level

    def test_no_op_on_non_dict(self):
        s = Settings()
        original = s.log_level
        s.apply_overrides("not a dict")  # type: ignore[arg-type]
        assert s.log_level == original

    def test_logging_level_string_to_enum(self):
        s = Settings()
        s.apply_overrides({"logging": {"level": "INFO"}})
        assert s.log_level == LogLevel.INFO

    def test_logging_level_lowercase_normalized(self):
        s = Settings()
        s.apply_overrides({"logging": {"level": "warning"}})
        assert s.log_level == LogLevel.WARNING

    def test_logging_max_size_mb_converted_to_bytes(self):
        s = Settings()
        s.apply_overrides({"logging": {"max_size_mb": 5}})
        assert s.log_file_max_size == 5 * 1024 * 1024

    def test_ui_overrides(self):
        s = Settings()
        s.apply_overrides({"ui": {"streaming_enabled": False, "show_tool_details": True}})
        assert s.streaming_enabled is False
        assert s.show_tool_details is True

    def test_model_overrides(self):
        s = Settings()
        s.apply_overrides({"model": {"default_provider": "anthropic", "max_context_tokens": 16000}})
        assert s.default_model_provider == "anthropic"
        assert s.max_context_tokens == 16000

    def test_caching_overrides(self):
        s = Settings()
        s.apply_overrides({"caching": {"enabled": False, "ttl_seconds": 60}})
        assert s.enable_caching is False
        assert s.cache_ttl == 60

    def test_concurrency_overrides(self):
        s = Settings()
        s.apply_overrides({"concurrency": {"max_concurrent_requests": 5, "request_timeout": 60}})
        assert s.max_concurrent_requests == 5
        assert s.request_timeout == 60

    def test_file_safety_overrides(self):
        s = Settings()
        s.apply_overrides({"file_safety": {"enabled": False, "max_file_size_bytes": 2048}})
        assert s.enable_file_safety_checks is False
        assert s.max_file_size_bytes == 2048

    def test_deile_md_user_path_string_to_path(self):
        s = Settings()
        s.apply_overrides({"deile_md": {"user_path": "/tmp/MY_DEILE.md"}})
        assert s.deile_md_user_path == Path("/tmp/MY_DEILE.md")

    def test_deile_md_user_path_null_preserved(self):
        s = Settings()
        s.apply_overrides({"deile_md": {"user_path": None}})
        assert s.deile_md_user_path is None

    def test_environment_top_level(self):
        s = Settings()
        s.apply_overrides({"environment": "production"})
        assert s.environment == "production"

    def test_debug_top_level(self):
        s = Settings()
        s.apply_overrides({"debug": True})
        assert s.debug is True

    def test_unknown_keys_ignored(self):
        s = Settings()
        s.apply_overrides({"made_up": {"key": 1}, "another": "thing"})
        # No exception, no field changed.
        assert s.environment == "development"

    def test_unknown_nested_section_ignored(self):
        s = Settings()
        s.apply_overrides({"logging": {"made_up_key": "value"}})
        # logging.level not in payload — default preserved.
        assert s.log_level == LogLevel.DEBUG

    def test_invalid_log_level_keeps_default(self):
        s = Settings()
        original = s.log_level
        records: list[logging.LogRecord] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _CaptureHandler(level=logging.WARNING)
        logger = logging.getLogger("deile.config.settings")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            s.apply_overrides({"logging": {"level": "NOT_A_LEVEL"}})
        finally:
            logger.removeHandler(handler)

        assert s.log_level == original
        assert any("logging.level" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# get_settings() — layered flow
# ---------------------------------------------------------------------------


class TestGetSettingsLayered:
    def test_defaults_when_no_files(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        s = get_settings()
        assert s.log_level == LogLevel.DEBUG  # dataclass default

    def test_user_layer_applied(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".deile").mkdir(parents=True)
        (home / ".deile" / "settings.json").write_text(
            json.dumps({"logging": {"level": "INFO"}}), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        s = get_settings()
        assert s.log_level == LogLevel.INFO

    def test_project_overrides_user(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".deile").mkdir(parents=True)
        (home / ".deile" / "settings.json").write_text(
            json.dumps({"logging": {"level": "INFO"}, "ui": {"streaming_enabled": True}}),
            encoding="utf-8",
        )
        project = tmp_path / "project"
        (project / ".deile").mkdir(parents=True)
        (project / ".deile" / "settings.json").write_text(
            json.dumps({"logging": {"level": "DEBUG"}}), encoding="utf-8"
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        s = get_settings()
        assert s.log_level == LogLevel.DEBUG          # project wins
        assert s.streaming_enabled is True            # inherited from user

    def test_legacy_fallback_when_no_new_layers(self, monkeypatch, tmp_path):
        legacy = tmp_path / "config"
        legacy.mkdir()
        legacy_file = legacy / "settings.json"
        legacy_file.write_text(json.dumps({"environment": "staging"}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        records: list[logging.LogRecord] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _CaptureHandler(level=logging.WARNING)
        logger = logging.getLogger("deile.config.settings")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            s = get_settings()
        finally:
            logger.removeHandler(handler)

        assert s.environment == "staging"
        assert any("legacy" in r.getMessage() for r in records)

    def test_new_layers_take_precedence_over_legacy(self, monkeypatch, tmp_path):
        legacy = tmp_path / "config"
        legacy.mkdir()
        (legacy / "settings.json").write_text(
            json.dumps({"environment": "from-legacy"}), encoding="utf-8"
        )
        home = tmp_path / "home"
        (home / ".deile").mkdir(parents=True)
        (home / ".deile" / "settings.json").write_text(
            json.dumps({"environment": "from-user"}), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        s = get_settings()
        assert s.environment == "from-user"

    def test_singleton_caches_result(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_reset_settings_invalidates_cache(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        first = get_settings()
        reset_settings()
        second = get_settings()
        assert first is not second
