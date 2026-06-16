"""Tests for resolve_stage_timeout_s and resolve_stage_max_retries (issue #391)."""

import pytest

from deile.orchestration.pipeline.dispatch_resolver import (
    BUILT_IN_MAX_RETRIES,
    BUILT_IN_TIMEOUT_S_CLAUDE,
    BUILT_IN_TIMEOUT_S_DEILE,
    PIPELINE_STAGES,
    resolve_stage_max_retries,
    resolve_stage_timeout_s,
)


def _clear_timeout_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)


def _clear_retries_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_RETRIES_{stage.upper()}", raising=False)


# ---------------------------------------------------------------------------
# Built-in constants
# ---------------------------------------------------------------------------


def test_built_in_constants_values():
    assert BUILT_IN_TIMEOUT_S_CLAUDE == 1800
    assert BUILT_IN_TIMEOUT_S_DEILE == 900
    assert BUILT_IN_MAX_RETRIES == 3


# ---------------------------------------------------------------------------
# resolve_stage_timeout_s — built-in fallback
# ---------------------------------------------------------------------------


def test_timeout_deile_worker_default(monkeypatch):
    """Sem env var, deile-worker stages → BUILT_IN_TIMEOUT_S_DEILE."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "deile-worker")
    # classify dispatches to deile-worker by default
    result = resolve_stage_timeout_s("classify")
    assert result == BUILT_IN_TIMEOUT_S_DEILE


def test_timeout_claude_worker_default(monkeypatch):
    """Sem env var, claude-worker stages → BUILT_IN_TIMEOUT_S_CLAUDE."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    result = resolve_stage_timeout_s("implement")
    assert result == BUILT_IN_TIMEOUT_S_CLAUDE


def test_timeout_env_per_stage_overrides_default(monkeypatch):
    """DEILE_PIPELINE_TIMEOUT_S_<STAGE> wins over built-in."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "600")
    result = resolve_stage_timeout_s("implement")
    assert result == 600


def test_timeout_env_per_stage_is_int(monkeypatch):
    """Env var is parsed as int."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_REFINE", "1234")
    assert resolve_stage_timeout_s("refine") == 1234


def test_timeout_env_zero_is_invalid(monkeypatch):
    """Env var value 0 → ValueError (timeout must be > 0)."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "0")
    with pytest.raises(ValueError):
        resolve_stage_timeout_s("classify")


def test_timeout_env_negative_is_invalid(monkeypatch):
    """Env var negative value → ValueError."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "-1")
    with pytest.raises(ValueError):
        resolve_stage_timeout_s("classify")


def test_timeout_env_non_numeric_is_invalid(monkeypatch):
    """Env var non-numeric → ValueError."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_CLASSIFY", "garbage")
    with pytest.raises(ValueError):
        resolve_stage_timeout_s("classify")


def test_timeout_empty_env_falls_through(monkeypatch):
    """Empty env var treated as unset — falls through to default."""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT", "")
    # No exception, returns built-in
    result = resolve_stage_timeout_s("implement")
    assert result in (BUILT_IN_TIMEOUT_S_CLAUDE, BUILT_IN_TIMEOUT_S_DEILE)


def test_timeout_invalid_stage_raises(monkeypatch):
    """Unknown stage → ValueError (programming bug)."""
    _clear_timeout_env(monkeypatch)
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stage_timeout_s("not_a_stage")


def test_timeout_all_stages_return_positive_int(monkeypatch):
    """All 5 stages return a positive integer with no overrides."""
    _clear_timeout_env(monkeypatch)
    for stage in PIPELINE_STAGES:
        result = resolve_stage_timeout_s(stage)
        assert isinstance(
            result, int
        ), f"stage {stage!r}: expected int, got {type(result)}"
        assert result > 0, f"stage {stage!r}: expected > 0, got {result}"


# ---------------------------------------------------------------------------
# resolve_stage_max_retries — fallback chain
# ---------------------------------------------------------------------------


def test_retries_built_in_default(monkeypatch):
    """No overrides → BUILT_IN_MAX_RETRIES (3)."""
    _clear_retries_env(monkeypatch)
    assert resolve_stage_max_retries("implement") == BUILT_IN_MAX_RETRIES


def test_retries_env_per_stage(monkeypatch):
    """DEILE_PIPELINE_RETRIES_<STAGE> overrides built-in."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_IMPLEMENT", "5")
    assert resolve_stage_max_retries("implement") == 5


def test_retries_env_zero_is_valid(monkeypatch):
    """0 retries is valid (no retry on failure)."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_CLASSIFY", "0")
    assert resolve_stage_max_retries("classify") == 0


def test_retries_env_negative_raises(monkeypatch):
    """Negative retries → ValueError."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_CLASSIFY", "-1")
    with pytest.raises(ValueError):
        resolve_stage_max_retries("classify")


def test_retries_env_non_numeric_raises(monkeypatch):
    """Non-numeric retries → ValueError."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_CLASSIFY", "lots")
    with pytest.raises(ValueError):
        resolve_stage_max_retries("classify")


def test_retries_empty_env_falls_through(monkeypatch):
    """Empty env var treated as unset."""
    _clear_retries_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_RETRIES_CLASSIFY", "")
    assert resolve_stage_max_retries("classify") == BUILT_IN_MAX_RETRIES


def test_retries_invalid_stage_raises(monkeypatch):
    """Unknown stage → ValueError."""
    _clear_retries_env(monkeypatch)
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stage_max_retries("bad_stage")


def test_retries_all_stages_return_nonneg_int(monkeypatch):
    """All 5 stages return a non-negative integer with no overrides."""
    _clear_retries_env(monkeypatch)
    for stage in PIPELINE_STAGES:
        result = resolve_stage_max_retries(stage)
        assert isinstance(result, int), f"stage {stage!r}: expected int"
        assert result >= 0, f"stage {stage!r}: expected >= 0, got {result}"


# ---------------------------------------------------------------------------
# Settings fallback (via monkeypatched settings singleton)
# ---------------------------------------------------------------------------


def test_timeout_settings_per_stage_fallback(monkeypatch):
    """pipeline_timeout_s_implement from settings wins over built-in."""
    _clear_timeout_env(monkeypatch)
    from deile.config import settings as _settings_mod

    monkeypatch.setattr(_settings_mod, "_settings", None)
    # Inject a mock settings with the per-stage field set
    import types

    mock_s = types.SimpleNamespace(
        pipeline_timeout_s_implement=750,
        pipeline_timeout_s_classify=None,
        pipeline_timeout_s_refine=None,
        pipeline_timeout_s_pr_review=None,
        pipeline_timeout_s_follow_ups=None,
        pipeline_claude_timeout=1800,
        pipeline_deile_timeout=None,
        pipeline_dispatch_mode="deile-worker",
        pipeline_dispatcher_implement=None,
        pipeline_dispatcher_classify=None,
        pipeline_dispatcher_refine=None,
        pipeline_dispatcher_pr_review=None,
        pipeline_dispatcher_follow_ups=None,
    )
    monkeypatch.setattr(_settings_mod, "_settings", mock_s)
    assert resolve_stage_timeout_s("implement") == 750
    monkeypatch.setattr(_settings_mod, "_settings", None)


def test_retries_settings_per_stage_fallback(monkeypatch):
    """pipeline_retries_implement from settings wins over built-in."""
    _clear_retries_env(monkeypatch)
    import types

    from deile.config import settings as _settings_mod

    mock_s = types.SimpleNamespace(
        pipeline_retries_implement=7,
        pipeline_retries_classify=None,
        pipeline_retries_refine=None,
        pipeline_retries_pr_review=None,
        pipeline_retries_follow_ups=None,
        pipeline_default_max_retries=None,
        pipeline_dispatch_mode="deile-worker",
        pipeline_dispatcher_implement=None,
        pipeline_dispatcher_classify=None,
        pipeline_dispatcher_refine=None,
        pipeline_dispatcher_pr_review=None,
        pipeline_dispatcher_follow_ups=None,
        pipeline_timeout_s_implement=None,
        pipeline_timeout_s_classify=None,
        pipeline_timeout_s_refine=None,
        pipeline_timeout_s_pr_review=None,
        pipeline_timeout_s_follow_ups=None,
        pipeline_claude_timeout=1800,
        pipeline_deile_timeout=None,
    )
    monkeypatch.setattr(_settings_mod, "_settings", mock_s)
    assert resolve_stage_max_retries("implement") == 7
    monkeypatch.setattr(_settings_mod, "_settings", None)


def test_retries_global_default_fallback(monkeypatch):
    """pipeline_default_max_retries from settings used when no per-stage."""
    _clear_retries_env(monkeypatch)
    import types

    from deile.config import settings as _settings_mod

    mock_s = types.SimpleNamespace(
        pipeline_retries_implement=None,
        pipeline_retries_classify=None,
        pipeline_retries_refine=None,
        pipeline_retries_pr_review=None,
        pipeline_retries_follow_ups=None,
        pipeline_default_max_retries=10,
        pipeline_dispatch_mode="deile-worker",
        pipeline_dispatcher_implement=None,
        pipeline_dispatcher_classify=None,
        pipeline_dispatcher_refine=None,
        pipeline_dispatcher_pr_review=None,
        pipeline_dispatcher_follow_ups=None,
        pipeline_timeout_s_implement=None,
        pipeline_timeout_s_classify=None,
        pipeline_timeout_s_refine=None,
        pipeline_timeout_s_pr_review=None,
        pipeline_timeout_s_follow_ups=None,
        pipeline_claude_timeout=1800,
        pipeline_deile_timeout=None,
    )
    monkeypatch.setattr(_settings_mod, "_settings", mock_s)
    assert resolve_stage_max_retries("implement") == 10
    monkeypatch.setattr(_settings_mod, "_settings", None)
