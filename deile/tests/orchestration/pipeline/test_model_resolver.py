"""Tests for `resolve_stage_model` (issue #305).

The resolver is the single source of truth for "what model does this stage
use?". Per the design notes in the module docstring:

- Returns the per-stage override when set.
- Returns ``None`` when no override is set (caller falls back to the global
  default; sending ``None`` keeps the wire payload minimal).
- Treats empty/whitespace as unset (defensive against partial writes).
- Raises ``ValueError`` on an unknown stage name (programming bug, not user
  input — implementer.py passes literals).
"""

from __future__ import annotations

import pytest

from deile.config.settings import reset_settings
from deile.orchestration.pipeline.model_resolver import (
    PIPELINE_STAGES,
    resolve_stage_cli_model,
    resolve_stage_model,
)


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Each test starts with a fresh Settings singleton.

    Without this, env-var overrides from one test leak into the next via the
    process-wide singleton cache.
    """
    # Clear any pipeline-model env vars so a developer-set var doesn't break
    # the deterministic "unset → None" tests.
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_MODEL_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PREFERRED_MODEL", raising=False)
    reset_settings()
    yield
    reset_settings()


class TestPipelineStages:
    def test_canonical_set_matches_settings_field_names(self):
        # The settings.py fields are `pipeline_model_<stage>`. PIPELINE_STAGES
        # must match exactly — a drift breaks the resolver silently.
        from deile.config.settings import Settings

        for stage in PIPELINE_STAGES:
            assert hasattr(Settings(), f"pipeline_model_{stage}"), (
                f"PIPELINE_STAGES has {stage!r} but Settings lacks "
                f"pipeline_model_{stage}"
            )

    def test_canonical_set_size(self):
        # Sanity: exactly 5 stages. If this fails, the panel TUI's `[1]-[5]`
        # picker shortcuts and StageModelsView fallback list also need updating.
        assert len(PIPELINE_STAGES) == 5


class TestResolveStageModel:
    def test_unset_stage_returns_none(self):
        assert resolve_stage_model("implement") is None

    def test_set_stage_returns_override(self, monkeypatch):
        monkeypatch.setenv(
            "DEILE_PIPELINE_MODEL_IMPLEMENT", "anthropic:claude-opus-4-8"
        )
        reset_settings()
        assert resolve_stage_model("implement") == "anthropic:claude-opus-4-8"
        # Other stages stay None — override is per-stage, not global.
        assert resolve_stage_model("classify") is None
        assert resolve_stage_model("refine") is None

    def test_global_preferred_does_not_leak_into_resolver(self, monkeypatch):
        """The resolver returns None when there's no per-stage override even
        if a global preferred_model is set — so the dispatch payload stays
        minimal and the worker resolves its own default."""
        monkeypatch.setenv("DEILE_PREFERRED_MODEL", "deepseek:deepseek-v4-pro")
        reset_settings()
        assert resolve_stage_model("implement") is None

    def test_empty_string_treated_as_unset(self, monkeypatch):
        """Defensive: a partial write that left "" in the JSON must collapse
        to None, not bleed through as a literal empty slug."""
        from deile.config.settings import get_settings

        reset_settings()
        # Patch the singleton directly to simulate a write that bypassed the
        # strict converter (the loose `_apply_nested_dict` + `_set_typed`
        # path could store "" if a future code path is added).
        get_settings().pipeline_model_implement = ""
        assert resolve_stage_model("implement") is None

    def test_unknown_stage_raises_value_error(self):
        with pytest.raises(ValueError) as exc_info:
            resolve_stage_model("garbage")
        assert "garbage" in str(exc_info.value)
        assert "classify" in str(exc_info.value)  # lists valid options


class TestResolveStageCliModel:
    """CLI workers consomem ``cli_model`` (string livre via env), não slug."""

    def test_unset_returns_none(self):
        assert resolve_stage_cli_model("implement") is None

    def test_free_string_via_env_is_returned_verbatim(self, monkeypatch):
        # Id nativo de CLI que o regex provider:model REJEITARIA — deve passar
        # intacto porque o CLI resolver lê a env var como string livre.
        monkeypatch.setenv(
            "DEILE_PIPELINE_MODEL_IMPLEMENT", "openrouter/deepseek/deepseek-chat"
        )
        reset_settings()
        assert (
            resolve_stage_cli_model("implement") == "openrouter/deepseek/deepseek-chat"
        )
        # Per-stage: outras etapas continuam None.
        assert resolve_stage_cli_model("classify") is None

    def test_global_preferred_is_used_as_fallback(self, monkeypatch):
        # Sem override por etapa, cai no DEILE_PREFERRED_MODEL global (livre).
        monkeypatch.setenv("DEILE_PREFERRED_MODEL", "qwen3-coder-plus")
        reset_settings()
        assert resolve_stage_cli_model("implement") == "qwen3-coder-plus"

    def test_per_stage_overrides_global(self, monkeypatch):
        monkeypatch.setenv("DEILE_PREFERRED_MODEL", "qwen3-coder-plus")
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT", "gpt-5.5-codex")
        reset_settings()
        assert resolve_stage_cli_model("implement") == "gpt-5.5-codex"
        # Etapa sem override usa o global.
        assert resolve_stage_cli_model("classify") == "qwen3-coder-plus"

    def test_whitespace_only_collapses_to_none(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT", "   ")
        reset_settings()
        assert resolve_stage_cli_model("implement") is None

    def test_unknown_stage_raises_value_error(self):
        with pytest.raises(ValueError, match="garbage"):
            resolve_stage_cli_model("garbage")
