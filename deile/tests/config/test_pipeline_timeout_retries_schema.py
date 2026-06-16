"""Tests for per-stage timeout / retries settings schema (issue #391).

Mirrors test_pipeline_dispatchers_schema.py and test_settings_pipeline_models.py.
"""

import pytest

from deile.config.settings import (
    Settings,
    _to_optional_nonneg_int,
    _to_optional_pos_int,
)

# ---------------------------------------------------------------------------
# Converter unit tests
# ---------------------------------------------------------------------------


class TestToOptionalPosInt:
    def test_none_returns_none(self):
        assert _to_optional_pos_int(None) is None

    def test_valid_positive(self):
        assert _to_optional_pos_int(42) == 42
        assert _to_optional_pos_int("600") == 600
        assert _to_optional_pos_int(1) == 1

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="> 0"):
            _to_optional_pos_int(0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="> 0"):
            _to_optional_pos_int(-1)

    def test_bool_raises(self):
        with pytest.raises(TypeError):
            _to_optional_pos_int(True)

    def test_non_numeric_string_raises(self):
        with pytest.raises((ValueError, TypeError)):
            _to_optional_pos_int("abc")


class TestToOptionalNonnegInt:
    def test_none_returns_none(self):
        assert _to_optional_nonneg_int(None) is None

    def test_zero_is_valid(self):
        assert _to_optional_nonneg_int(0) == 0
        assert _to_optional_nonneg_int("0") == 0

    def test_valid_positive(self):
        assert _to_optional_nonneg_int(3) == 3
        assert _to_optional_nonneg_int("10") == 10

    def test_negative_raises(self):
        with pytest.raises(ValueError, match=">= 0"):
            _to_optional_nonneg_int(-1)

    def test_bool_raises(self):
        with pytest.raises(TypeError):
            _to_optional_nonneg_int(False)

    def test_non_numeric_string_raises(self):
        with pytest.raises((ValueError, TypeError)):
            _to_optional_nonneg_int("abc")


# ---------------------------------------------------------------------------
# Settings dataclass defaults
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    def test_timeout_fields_default_none(self):
        s = Settings()
        for stage in ("classify", "refine", "implement", "pr_review", "follow_ups"):
            assert getattr(s, f"pipeline_timeout_s_{stage}") is None

    def test_retries_fields_default_none(self):
        s = Settings()
        for stage in ("classify", "refine", "implement", "pr_review", "follow_ups"):
            assert getattr(s, f"pipeline_retries_{stage}") is None

    def test_global_defaults_are_none(self):
        s = Settings()
        assert s.pipeline_deile_timeout is None
        assert s.pipeline_default_max_retries is None


# ---------------------------------------------------------------------------
# apply_overrides — JSON path loading
# ---------------------------------------------------------------------------


class TestApplyOverrides:
    def test_timeout_per_stage_json(self):
        s = Settings()
        s.apply_overrides(
            {
                "pipeline": {
                    "timeouts_s": {
                        "implement": 600,
                        "pr_review": 1800,
                    }
                }
            }
        )
        assert s.pipeline_timeout_s_implement == 600
        assert s.pipeline_timeout_s_pr_review == 1800
        # Untouched stages remain None
        assert s.pipeline_timeout_s_classify is None

    def test_retries_per_stage_json(self):
        s = Settings()
        s.apply_overrides(
            {
                "pipeline": {
                    "retries": {
                        "implement": 2,
                        "classify": 5,
                    }
                }
            }
        )
        assert s.pipeline_retries_implement == 2
        assert s.pipeline_retries_classify == 5
        assert s.pipeline_retries_refine is None

    def test_retries_zero_is_valid(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"retries": {"implement": 0}}})
        assert s.pipeline_retries_implement == 0

    def test_timeout_zero_is_rejected(self):
        """Zero timeout should be rejected; field stays None."""
        s = Settings()
        s.apply_overrides({"pipeline": {"timeouts_s": {"implement": 0}}})
        assert s.pipeline_timeout_s_implement is None

    def test_timeout_negative_is_rejected(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"timeouts_s": {"implement": -10}}})
        assert s.pipeline_timeout_s_implement is None

    def test_global_deile_timeout(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"default_timeout_s_deile": 450}})
        assert s.pipeline_deile_timeout == 450

    def test_global_max_retries(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"default_max_retries": 5}})
        assert s.pipeline_default_max_retries == 5

    def test_global_max_retries_zero(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"default_max_retries": 0}})
        assert s.pipeline_default_max_retries == 0


# ---------------------------------------------------------------------------
# Env var loading (_apply_env_overrides)
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_timeout_env_vars(self, monkeypatch):
        from deile.config.settings import _apply_env_overrides

        monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "900")
        monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "300")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_timeout_s_implement == 900
        assert s.pipeline_timeout_s_classify == 300
        assert s.pipeline_timeout_s_refine is None

    def test_retries_env_vars(self, monkeypatch):
        from deile.config.settings import _apply_env_overrides

        monkeypatch.setenv("DEILE_PIPELINE_RETRIES_IMPLEMENT", "2")
        monkeypatch.setenv("DEILE_PIPELINE_RETRIES_PR_REVIEW", "0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_retries_implement == 2
        assert s.pipeline_retries_pr_review == 0
        assert s.pipeline_retries_classify is None

    def test_timeout_zero_rejected_by_env(self, monkeypatch):
        """Zero timeout env var is silently ignored (stays None)."""
        from deile.config.settings import _apply_env_overrides

        monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_timeout_s_implement is None

    def test_global_deile_timeout_env(self, monkeypatch):
        from deile.config.settings import _apply_env_overrides

        monkeypatch.setenv("DEILE_PIPELINE_DEILE_TIMEOUT", "600")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_deile_timeout == 600

    def test_global_max_retries_env(self, monkeypatch):
        from deile.config.settings import _apply_env_overrides

        monkeypatch.setenv("DEILE_PIPELINE_DEFAULT_MAX_RETRIES", "7")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_default_max_retries == 7
