"""Tests for `resolve_stage_reasoning` (per-stage reasoning effort).

Mirrors `test_model_resolver` but the resolver also folds in the GLOBAL
``reasoning_effort`` (like ``resolve_stage_timeout_s``), so:

- per-stage override wins;
- else the global ``reasoning_effort`` applies;
- else ``None`` (provider default).
"""

from __future__ import annotations

import pytest

from deile.config.settings import reset_settings
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES
from deile.orchestration.pipeline.reasoning_resolver import \
    resolve_stage_reasoning


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_REASONING_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.mark.unit
def test_unset_returns_none():
    assert resolve_stage_reasoning("implement") is None


@pytest.mark.unit
def test_per_stage_override(monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "xhigh")
    reset_settings()
    assert resolve_stage_reasoning("implement") == "xhigh"
    # other stages still None (no leak)
    assert resolve_stage_reasoning("classify") is None


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
    # The strict converter rejects unknown tokens → attribute stays None.
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "bogus-level")
    reset_settings()
    assert resolve_stage_reasoning("implement") is None
