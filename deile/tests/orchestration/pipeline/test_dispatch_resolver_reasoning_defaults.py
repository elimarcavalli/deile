"""Tests for opinionated per-stage reasoning_effort defaults (issue #450).

`_STAGE_DEFAULT_REASONING_EFFORT` provides a 3rd fallback level in
`resolve_stage_reasoning` (between global settings and None) so each stage
has a sensible default when operator and user have not configured
reasoning_effort explicitly.
"""

from __future__ import annotations

import pytest

from deile.config.settings import reset_settings
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES
from deile.orchestration.pipeline.reasoning_resolver import (
    _STAGE_DEFAULT_REASONING_EFFORT,
    resolve_stage_reasoning,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_REASONING_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
    reset_settings()
    yield
    reset_settings()


# ---------------------------------------------------------------------------
# Stage defaults (level 3 — no operator override)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "stage,expected",
    [
        ("classify", "low"),
        ("refine", "low"),
        ("implement", "medium"),
        ("pr_review", "high"),
        ("follow_ups", "low"),
    ],
)
def test_stage_default_returned_when_no_override(stage, expected):
    assert resolve_stage_reasoning(stage) == expected


@pytest.mark.unit
def test_stage_defaults_mapping_covers_all_stages():
    assert set(_STAGE_DEFAULT_REASONING_EFFORT.keys()) == set(PIPELINE_STAGES)


# ---------------------------------------------------------------------------
# Level 1 — per-stage settings override (via env var → settings object)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_per_stage_env_overrides_default(monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "max")
    reset_settings()
    assert resolve_stage_reasoning("implement") == "max"
    # other stages still get their default
    assert resolve_stage_reasoning("classify") == "low"


@pytest.mark.unit
def test_per_stage_env_overrides_pr_review_default(monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_PR_REVIEW", "low")
    reset_settings()
    assert resolve_stage_reasoning("pr_review") == "low"


# ---------------------------------------------------------------------------
# Level 2 — global settings override
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_global_reasoning_effort_overrides_stage_default(monkeypatch):
    monkeypatch.setenv("DEILE_REASONING_EFFORT", "xhigh")
    reset_settings()
    for stage in PIPELINE_STAGES:
        assert resolve_stage_reasoning(stage) == "xhigh"


# ---------------------------------------------------------------------------
# Precedence: level 1 beats level 2 beats level 3
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_per_stage_wins_over_global(monkeypatch):
    monkeypatch.setenv("DEILE_REASONING_EFFORT", "low")
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_PR_REVIEW", "max")
    reset_settings()
    assert resolve_stage_reasoning("pr_review") == "max"
    # other stages get global
    assert resolve_stage_reasoning("classify") == "low"


@pytest.mark.unit
def test_global_wins_over_stage_default(monkeypatch):
    monkeypatch.setenv("DEILE_REASONING_EFFORT", "medium")
    reset_settings()
    # pr_review default is "high" but global overrides it
    assert resolve_stage_reasoning("pr_review") == "medium"


# ---------------------------------------------------------------------------
# Operator override via settings.json must not disable the default
# (i.e., the function returns the per-stage or global value when set,
# and the stage default when neither is set — the default is never None
# for any known stage)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_stage_has_none_as_default():
    for stage in PIPELINE_STAGES:
        result = resolve_stage_reasoning(stage)
        assert result is not None, f"stage {stage!r} returned None — missing default"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_stage_raises():
    with pytest.raises(ValueError):
        resolve_stage_reasoning("nonexistent_stage")
