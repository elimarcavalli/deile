"""Settings loading for reasoning effort (env vars + nested settings.json).

Covers the converter ``_to_optional_reasoning_effort`` and the two load paths
(``_ENV_OVERRIDES`` and the nested ``_OVERRIDE_HANDLERS``/``apply_overrides``).
"""

from __future__ import annotations

import pytest

from deile.config.settings import Settings, get_settings, reset_settings
from deile.orchestration.pipeline.model_resolver import PIPELINE_STAGES


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_REASONING_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.mark.unit
def test_env_loads_global_and_per_stage(monkeypatch):
    monkeypatch.setenv("DEILE_REASONING_EFFORT", "high")
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "ultracode")
    reset_settings()
    s = get_settings()
    assert s.reasoning_effort == "high"
    assert s.pipeline_reasoning_implement == "ultracode"
    assert s.pipeline_reasoning_refine is None


@pytest.mark.unit
def test_env_invalid_value_dropped(monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_IMPLEMENT", "nope")
    reset_settings()
    # Strict converter rejects → stays at default None (graceful, with warning).
    assert get_settings().pipeline_reasoning_implement is None


@pytest.mark.unit
def test_env_value_is_lowercased(monkeypatch):
    monkeypatch.setenv("DEILE_PIPELINE_REASONING_PR_REVIEW", "XHigh")
    reset_settings()
    assert get_settings().pipeline_reasoning_pr_review == "xhigh"


@pytest.mark.unit
def test_nested_json_apply_overrides():
    s = Settings()
    s.apply_overrides({
        "model": {"reasoning_effort": "max"},
        "pipeline": {"reasoning": {"classify": "low", "implement": "high"}},
    })
    assert s.reasoning_effort == "max"
    assert s.pipeline_reasoning_classify == "low"
    assert s.pipeline_reasoning_implement == "high"
    assert s.pipeline_reasoning_follow_ups is None


@pytest.mark.unit
def test_nested_json_invalid_kept_previous():
    s = Settings()
    s.pipeline_reasoning_implement = "high"
    s.apply_overrides({"pipeline": {"reasoning": {"implement": "garbage"}}})
    # Invalid converter raise → apply_overrides keeps the previous value.
    assert s.pipeline_reasoning_implement == "high"
