"""Tests for the pipeline resume settings (issue #254).

Covers the four ``pipeline_resume_*`` knobs across the two current configuration
surfaces: dataclass defaults, the strict ``apply_overrides`` path, and the
layered ``_apply_nested_dict`` path (used by ``~/.deile/settings.json``).

Note: the ``DEILE_PIPELINE_RESUME_*`` env vars were removed in issue #309 fase 3
— they are no longer read and silently ignored if set.
"""

from __future__ import annotations

import logging

import pytest

from deile.config.settings import (Settings, _apply_env_overrides,
                                   _apply_nested_dict, reset_settings)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_settings()
    logging.disable(logging.NOTSET)
    yield
    reset_settings()


class TestDefaults:
    def test_defaults_match_spec(self):
        s = Settings()
        assert s.pipeline_resume_enabled is True
        assert s.pipeline_resume_interval == 0
        assert s.pipeline_resume_max_attempts == 10
        assert s.pipeline_resume_budget == 0


class TestApplyOverridesStrict:
    def test_all_four_keys(self):
        s = Settings()
        s.apply_overrides({
            "pipeline": {
                "resume_enabled": False,
                "resume_interval": 45,
                "resume_max_attempts": 5,
                "resume_budget": 3600,
            }
        })
        assert s.pipeline_resume_enabled is False
        assert s.pipeline_resume_interval == 45
        assert s.pipeline_resume_max_attempts == 5
        assert s.pipeline_resume_budget == 3600

    def test_negative_interval_rejected_keeps_default(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"resume_interval": -1}})
        # _to_nonneg_int raises → key skipped, default preserved.
        assert s.pipeline_resume_interval == 0

    def test_bool_string_coercion(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"resume_enabled": "false"}})
        assert s.pipeline_resume_enabled is False


class TestLayeredNestedDict:
    def test_nested_dict_applies(self):
        s = Settings()
        _apply_nested_dict(s, {
            "pipeline": {
                "resume_enabled": False,
                "resume_interval": 30,
                "resume_max_attempts": 7,
                "resume_budget": 1800,
            }
        })
        assert s.pipeline_resume_enabled is False
        assert s.pipeline_resume_interval == 30
        assert s.pipeline_resume_max_attempts == 7
        assert s.pipeline_resume_budget == 1800


class TestEnvOverrides:
    """DEILE_PIPELINE_RESUME_* env vars are ignored (removed in issue #309 fase 3).

    The canonical path is ``pipeline.resume_*`` in ``~/.deile/settings.json``.
    """

    def test_env_vars_ignored(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_ENABLED", "false")
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_INTERVAL", "120")
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_MAX_ATTEMPTS", "3")
        monkeypatch.setenv("DEILE_PIPELINE_RESUME_BUDGET", "900")
        s = Settings()
        _apply_env_overrides(s)
        # defaults preserved — env vars are silently ignored
        assert s.pipeline_resume_enabled is True
        assert s.pipeline_resume_interval == 0
        assert s.pipeline_resume_max_attempts == 10
        assert s.pipeline_resume_budget == 0
