"""Tests: trust-boundary on the project layer + legacy load_from_file allowlist (issue #125).

Covers gaps B and C from the #125 hardening work:
  - ``_load_layered_settings`` ignores ``<cwd>/.deile/settings.json`` when
    cwd is not in ``trust.project_layer_dirs`` and policy=='deny'.
  - It applies the project layer when cwd is allowlisted.
  - Default policy 'auto' applies the project layer with a warning.
  - ``Settings.load_from_file`` filters config_dict by an explicit allowlist
    and logs a warning for unknown keys.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

import pytest

from deile.config.settings import (
    LogLevel,
    Settings,
    _load_layered_settings,
    get_settings,
    reset_settings,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_settings()
    logging.disable(logging.NOTSET)
    yield
    reset_settings()


@contextlib.contextmanager
def _capture_settings_warnings():
    """Capture WARNING records from ``deile.config.settings`` regardless of
    global ``logging.disable()`` state — other tests in the suite trip that
    toggle and starve ``caplog``.
    """
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler(level=logging.WARNING)
    target = logging.getLogger("deile.config.settings")
    previous_disable = logging.root.manager.disable
    try:
        logging.disable(logging.NOTSET)
        target.addHandler(handler)
        target.setLevel(logging.WARNING)
        yield records
    finally:
        target.removeHandler(handler)
        logging.disable(previous_disable)


def _write_user_settings(home: Path, data: dict) -> Path:
    user_dir = home / ".deile"
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_project_settings(project: Path, data: dict) -> Path:
    proj_dir = project / ".deile"
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Trust boundary on project layer (gap B)
# ---------------------------------------------------------------------------


class TestProjectLayerTrust:
    def test_project_layer_ignored_when_not_allowlisted_and_policy_deny(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("DEILE_SETTINGS_FILE", raising=False)
        home = tmp_path / "home"
        _write_user_settings(
            home,
            {
                "logging": {"level": "INFO"},
                "trust": {
                    "project_layer_dirs": ["/some/other/dir"],
                    "project_layer_default": "deny",
                },
            },
        )
        project = tmp_path / "project"
        _write_project_settings(project, {"logging": {"level": "DEBUG"}})

        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        with _capture_settings_warnings() as records:
            s = _load_layered_settings()

        # User-layer level wins (project layer ignored).
        assert s.log_level == LogLevel.INFO
        assert any("ignoring project layer" in r.getMessage() for r in records)

    def test_project_layer_applied_when_allowlisted(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEILE_SETTINGS_FILE", raising=False)
        home = tmp_path / "home"
        project = tmp_path / "project"
        project.mkdir(parents=True, exist_ok=True)
        # Resolve here to match what _is_project_layer_trusted does internally.
        resolved_project = str(project.resolve())
        _write_user_settings(
            home,
            {
                "logging": {"level": "INFO"},
                "trust": {
                    "project_layer_dirs": [resolved_project],
                    "project_layer_default": "deny",
                },
            },
        )
        _write_project_settings(project, {"logging": {"level": "WARNING"}})

        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        s = _load_layered_settings()
        assert s.log_level == LogLevel.WARNING

    def test_default_policy_auto_applies_with_warning(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEILE_SETTINGS_FILE", raising=False)
        home = tmp_path / "home"
        # No explicit trust config => defaults: dirs=[], default='auto'.
        _write_user_settings(home, {"logging": {"level": "INFO"}})
        project = tmp_path / "project"
        _write_project_settings(project, {"logging": {"level": "WARNING"}})

        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        with _capture_settings_warnings() as records:
            s = _load_layered_settings()

        # In 'auto' grace-period, project still wins, but a loud warning
        # must be emitted.
        assert s.log_level == LogLevel.WARNING
        assert any("WITHOUT explicit trust" in r.getMessage() for r in records)

    def test_no_warning_when_allowlisted(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEILE_SETTINGS_FILE", raising=False)
        home = tmp_path / "home"
        project = tmp_path / "project"
        project.mkdir(parents=True, exist_ok=True)
        resolved_project = str(project.resolve())
        _write_user_settings(
            home,
            {
                "trust": {"project_layer_dirs": [resolved_project]},
            },
        )
        _write_project_settings(project, {"logging": {"level": "WARNING"}})

        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        with _capture_settings_warnings() as records:
            _load_layered_settings()

        # No warning: cwd is allowlisted.
        assert not any(
            "ignoring project layer" in r.getMessage()
            or "WITHOUT explicit trust" in r.getMessage()
            for r in records
        )

    def test_no_project_file_no_warning(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEILE_SETTINGS_FILE", raising=False)
        home = tmp_path / "home"
        _write_user_settings(home, {})
        project = tmp_path / "project"
        project.mkdir(parents=True, exist_ok=True)

        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        with _capture_settings_warnings() as records:
            _load_layered_settings()
        assert not any("project layer" in r.getMessage() for r in records)

    def test_get_settings_threaded_through_layered_loader(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEILE_SETTINGS_FILE", raising=False)
        """Smoke test that get_settings() uses the new gate."""
        home = tmp_path / "home"
        _write_user_settings(
            home,
            {
                "logging": {"level": "INFO"},
                "trust": {"project_layer_default": "deny"},
            },
        )
        project = tmp_path / "project"
        _write_project_settings(project, {"logging": {"level": "DEBUG"}})

        monkeypatch.chdir(project)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        s = get_settings()
        # Project layer ignored => user's INFO wins.
        assert s.log_level == LogLevel.INFO


# ---------------------------------------------------------------------------
# Legacy load_from_file allowlist (gap C)
# ---------------------------------------------------------------------------


class TestLegacyLoadFromFileAllowlist:
    def test_unknown_key_dropped_with_warning(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "enable_file_safety_checks": False,
                    "random_attr": "x",
                }
            ),
            encoding="utf-8",
        )
        with _capture_settings_warnings() as records:
            s = Settings.load_from_file(path)
        # `enable_file_safety_checks` IS in the allowlist (mapped from
        # file_safety.enabled in _OVERRIDE_HANDLERS) — applied.
        assert s.enable_file_safety_checks is False
        # `random_attr` is NOT in the allowlist — dropped.
        assert not hasattr(s, "random_attr")
        # Warning must explicitly mention the dropped key.
        assert any("random_attr" in r.getMessage() for r in records)

    def test_dangerous_key_working_directory_dropped(self, tmp_path):
        """`working_directory` is intentionally NOT in the allowlist —
        a hostile legacy file must not be able to redirect it."""
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"working_directory": "/etc"}),
            encoding="utf-8",
        )
        with _capture_settings_warnings() as records:
            s = Settings.load_from_file(path)
        # Default cwd is preserved.
        assert s.working_directory != Path("/etc")
        assert any("working_directory" in r.getMessage() for r in records)

    def test_api_keys_still_stripped(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"api_keys": {"OPENAI_API_KEY": "sk-x"}}),
            encoding="utf-8",
        )
        s = Settings.load_from_file(path)
        # api_keys reload from env; the literal "sk-x" must NOT survive.
        assert s.api_keys.get("OPENAI_API_KEY") != "sk-x"

    def test_returns_defaults_when_json_is_not_object(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with _capture_settings_warnings():
            s = Settings.load_from_file(path)
        # Defaults preserved.
        assert s.app_name == "DEILE"

    def test_invalid_log_level_falls_back_to_default(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"log_level": "NOT_A_LEVEL"}),
            encoding="utf-8",
        )
        with _capture_settings_warnings():
            s = Settings.load_from_file(path)
        assert s.log_level == LogLevel.DEBUG  # dataclass default

    def test_valid_log_level_applied(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"log_level": "WARNING"}), encoding="utf-8")
        s = Settings.load_from_file(path)
        assert s.log_level == LogLevel.WARNING
