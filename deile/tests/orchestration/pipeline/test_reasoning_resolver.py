"""Tests for `resolve_stage_reasoning` (per-stage reasoning effort).

Mirrors `test_model_resolver` but the resolver also folds in the GLOBAL
``reasoning_effort`` (like ``resolve_stage_timeout_s``) and the opinionated
stage defaults (issue #450), so:

- per-stage override wins;
- else the global ``reasoning_effort`` applies;
- else the opinionated stage default (classify/refine/follow_ups→low,
  implement→medium, pr_review→high);
- else ``None`` (provider default — unreachable for known stages).
"""

from __future__ import annotations

import pytest

from deile.config.settings import reset_settings
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES
from deile.orchestration.pipeline.reasoning_resolver import resolve_stage_reasoning


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_REASONING_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.mark.unit
def test_unset_returns_stage_default():
    # With no override, each stage returns its opinionated default (issue #450)
    assert resolve_stage_reasoning("implement") == "medium"
    assert resolve_stage_reasoning("pr_review") == "high"
    assert resolve_stage_reasoning("classify") == "low"


@pytest.mark.unit
def test_per_stage_override(monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "xhigh")
    reset_settings()
    assert resolve_stage_reasoning("implement") == "xhigh"
    # other stages still return their own defaults (no leak from this override)
    assert resolve_stage_reasoning("classify") == "low"


@pytest.mark.unit
def test_global_fallback(monkeypatch):
    monkeypatch.setenv("DEILE_REASONING_EFFORT", "high")
    reset_settings()
    # every stage without its own override inherits the global
    for stage in PIPELINE_STAGES:
        assert resolve_stage_reasoning(stage) == "high"


@pytest.mark.unit
def test_per_stage_wins_over_global(monkeypatch):
    monkeypatch.setenv("DEILE_REASONING_EFFORT", "low")
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_PR_REVIEW", "max")
    reset_settings()
    assert resolve_stage_reasoning("pr_review") == "max"
    assert resolve_stage_reasoning("refine") == "low"  # global


@pytest.mark.unit
def test_unknown_stage_raises():
    with pytest.raises(ValueError):
        resolve_stage_reasoning("not_a_stage")


@pytest.mark.unit
def test_invalid_env_value_is_dropped(monkeypatch):
    # The strict converter rejects unknown tokens → attribute stays None,
    # so the resolver falls through to the opinionated stage default.
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "bogus-level")
    reset_settings()
    assert resolve_stage_reasoning("implement") == "medium"
