"""Tests for per-stage pipeline timeout/retries settings (issue #391).

Covers:
- 10 new fields default to ``None`` and round-trip through ``apply_overrides``.
- ``_to_optional_positive_int`` validator rejects 0/negatives (timeout must be > 0).
- ``_to_optional_nonneg_int`` validator accepts 0 (retries=0 means fail fast).
- Env-var overrides (``DEILE_PIPELINE_TIMEOUT_S_<STAGE>`` / ``DEILE_PIPELINE_RETRIES_<STAGE>``)
  take precedence over the JSON layer.
- The loose nested-dict loader (``_apply_nested_dict``) accepts the
  ``pipeline.timeouts_s.<stage>`` and ``pipeline.retries.<stage>`` paths.
"""

from __future__ import annotations

import pytest

from deile.config.settings import (
    Settings,
    _apply_env_overrides,
    _apply_nested_dict,
    _to_optional_nonneg_int,
    _to_optional_positive_int,
)


class TestOptionalPositiveIntValidator:
    """``_to_optional_positive_int`` is the strict converter for timeout settings."""

    def test_none_and_empty_collapse_to_none(self):
        assert _to_optional_positive_int(None) is None
        assert _to_optional_positive_int("") is None
        assert _to_optional_positive_int("   ") is None

    def test_valid_positive_int_passes(self):
        assert _to_optional_positive_int(300) == 300
        assert _to_optional_positive_int("1800") == 1800
        assert _to_optional_positive_int(1) == 1

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="> 0"):
            _to_optional_positive_int(0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="> 0"):
            _to_optional_positive_int(-1)

    def test_rejects_bool(self):
        with pytest.raises(TypeError):
            _to_optional_positive_int(True)

    def test_rejects_non_numeric_string(self):
        with pytest.raises(ValueError):
            _to_optional_positive_int("notanumber")


class TestOptionalNonnegIntValidator:
    """``_to_optional_nonneg_int`` is the strict converter for retries settings."""

    def test_none_and_empty_collapse_to_none(self):
        assert _to_optional_nonneg_int(None) is None
        assert _to_optional_nonneg_int("") is None
        assert _to_optional_nonneg_int("   ") is None

    def test_valid_nonneg_int_passes(self):
        assert _to_optional_nonneg_int(0) == 0
        assert _to_optional_nonneg_int(3) == 3
        assert _to_optional_nonneg_int("5") == 5

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match=">= 0"):
            _to_optional_nonneg_int(-1)

    def test_rejects_bool(self):
        with pytest.raises(TypeError):
            _to_optional_nonneg_int(False)

    def test_rejects_non_numeric_string(self):
        with pytest.raises(ValueError):
            _to_optional_nonneg_int("notanumber")


class TestSettingsDefaults:
    def test_timeout_fields_default_to_none(self):
        s = Settings()
        assert s.pipeline_timeout_s_classify is None
        assert s.pipeline_timeout_s_refine is None
        assert s.pipeline_timeout_s_implement is None
        assert s.pipeline_timeout_s_pr_review is None
        assert s.pipeline_timeout_s_follow_ups is None

    def test_retries_fields_default_to_none(self):
        s = Settings()
        assert s.pipeline_retries_classify is None
        assert s.pipeline_retries_refine is None
        assert s.pipeline_retries_implement is None
        assert s.pipeline_retries_pr_review is None
        assert s.pipeline_retries_follow_ups is None


class TestApplyOverrides:
    def test_timeout_round_trips_via_apply_overrides(self):
        s = Settings()
        s.apply_overrides({
            "pipeline": {
                "timeouts_s": {
                    "classify": 300,
                    "implement": 1800,
                }
            }
        })
        assert s.pipeline_timeout_s_classify == 300
        assert s.pipeline_timeout_s_implement == 1800
        # Unset fields stay None
        assert s.pipeline_timeout_s_refine is None

    def test_retries_round_trips_via_apply_overrides(self):
        s = Settings()
        s.apply_overrides({
            "pipeline": {
                "retries": {
                    "classify": 5,
                    "implement": 1,
                }
            }
        })
        assert s.pipeline_retries_classify == 5
        assert s.pipeline_retries_implement == 1
        assert s.pipeline_retries_refine is None

    def test_retries_zero_accepted(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"retries": {"implement": 0}}})
        assert s.pipeline_retries_implement == 0

    def test_timeout_zero_rejected_keeps_none(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"timeouts_s": {"classify": 0}}})
        # 0 is rejected by _to_optional_positive_int — field stays None
        assert s.pipeline_timeout_s_classify is None

    def test_timeout_negative_rejected(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"timeouts_s": {"refine": -100}}})
        assert s.pipeline_timeout_s_refine is None

    def test_retries_negative_rejected(self):
        s = Settings()
        s.apply_overrides({"pipeline": {"retries": {"pr_review": -1}}})
        assert s.pipeline_retries_pr_review is None


class TestNestedDictLoader:
    def test_timeout_via_apply_nested_dict(self):
        s = Settings()
        _apply_nested_dict(s, {"pipeline": {"timeouts_s": {"follow_ups": 600}}})
        assert s.pipeline_timeout_s_follow_ups == 600

    def test_retries_via_apply_nested_dict(self):
        s = Settings()
        _apply_nested_dict(s, {"pipeline": {"retries": {"classify": 2}}})
        assert s.pipeline_retries_classify == 2


class TestEnvVarOverrides:
    def test_timeout_env_var_sets_field(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "900")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_timeout_s_implement == 900

    def test_retries_env_var_sets_field(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_RETRIES_PR_REVIEW", "2")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_retries_pr_review == 2

    def test_timeout_invalid_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "garbage")
        s = Settings()
        s.pipeline_timeout_s_classify = 500  # pre-set a value
        _apply_env_overrides(s)
        # Env var is invalid, field keeps its pre-set value
        assert s.pipeline_timeout_s_classify == 500

    def test_retries_zero_env_var_accepted(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_RETRIES_CLASSIFY", "0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.pipeline_retries_classify == 0

    def test_all_stages_have_env_vars(self, monkeypatch):
        """All 5 stages have both timeout and retries env var entries."""
        stages = ("CLASSIFY", "REFINE", "IMPLEMENT", "PR_REVIEW", "FOLLOW_UPS")
        for stage in stages:
            monkeypatch.setenv(f"DEILE_PIPELINE_TIMEOUT_S_{stage}", "100")
            monkeypatch.setenv(f"DEILE_PIPELINE_RETRIES_{stage}", "1")
        s = Settings()
        _apply_env_overrides(s)
        for attr_suffix in ("classify", "refine", "implement", "pr_review", "follow_ups"):
            assert getattr(s, f"pipeline_timeout_s_{attr_suffix}") == 100, \
                f"pipeline_timeout_s_{attr_suffix} should be 100"
            assert getattr(s, f"pipeline_retries_{attr_suffix}") == 1, \
                f"pipeline_retries_{attr_suffix} should be 1"
