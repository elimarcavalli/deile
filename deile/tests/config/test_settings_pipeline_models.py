"""Tests for per-stage pipeline model settings (issue #305).

Covers:
- 5 new fields default to ``None`` and round-trip through ``apply_overrides``.
- Strict validator rejects malformed slugs (typo would silently route every
  dispatch to a non-existent model, surfacing only as a worker 5xx minutes
  later — fail fast at config load).
- Env-var overrides (``DEILE_PIPELINE_MODEL_<STAGE>``) take precedence over
  the JSON layer, mirroring how other DEILE_PIPELINE_* knobs behave.
- The loose nested-dict loader (``_apply_nested_dict``) also accepts the
  ``pipeline.models.<stage>`` paths, so layered settings load works.
"""

from __future__ import annotations

import pytest

from deile.config.settings import (Settings, _apply_env_overrides,
                                   _apply_nested_dict, _to_optional_model_slug)


class TestModelSlugValidator:
    """`_to_optional_model_slug` is the strict converter wired into
    `_OVERRIDE_HANDLERS`. A malformed slug here silently breaks dispatch."""

    def test_none_and_empty_collapse_to_none(self):
        assert _to_optional_model_slug(None) is None
        assert _to_optional_model_slug("") is None
        assert _to_optional_model_slug("   ") is None

    def test_valid_slugs_pass_through(self):
        assert _to_optional_model_slug("deepseek:deepseek-v4-pro") == \
            "deepseek:deepseek-v4-pro"
        assert _to_optional_model_slug("anthropic:claude-opus-4-8") == \
            "anthropic:claude-opus-4-8"
        # Dots, underscores and dashes in the model portion are allowed.
        assert _to_optional_model_slug("openai:gpt-4o-mini-2024_07_18") == \
            "openai:gpt-4o-mini-2024_07_18"

    def test_openrouter_slug_with_slash_is_accepted(self):
        """OpenRouter model ids embed the upstream vendor with a '/', e.g.
        ``openrouter:anthropic/claude-sonnet-4.6``. The slug regex must allow
        the '/' on the model side; otherwise the per-stage override is silently
        dropped (the validator raises and ``apply_overrides`` keeps the default).
        """
        assert _to_optional_model_slug("openrouter:anthropic/claude-sonnet-4.6") == \
            "openrouter:anthropic/claude-sonnet-4.6"
        assert _to_optional_model_slug("openrouter:deepseek/deepseek-chat") == \
            "openrouter:deepseek/deepseek-chat"
        assert _to_optional_model_slug("openrouter:qwen/qwen3-coder") == \
            "openrouter:qwen/qwen3-coder"

    def test_strips_surrounding_whitespace(self):
        assert _to_optional_model_slug("  deepseek:v4-pro  ") == "deepseek:v4-pro"

    @pytest.mark.parametrize("bad", [
        "garbage",            # missing colon
        "ANTHROPIC:opus",     # uppercase provider
        "provider:Model",     # uppercase in model
        ":model",             # empty provider
        "provider:",          # empty model
        "provider:with space",
        "provider:with\nnewline",
    ])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            _to_optional_model_slug(bad)

    def test_rejects_non_string(self):
        with pytest.raises(TypeError):
            _to_optional_model_slug(42)
        with pytest.raises(TypeError):
            _to_optional_model_slug(["deepseek:v4-pro"])


class TestSettingsDefaults:
    def test_five_new_fields_default_to_none(self):
        s = Settings()
        assert s.pipeline_model_classify is None
        assert s.pipeline_model_refine is None
        assert s.pipeline_model_implement is None
        assert s.pipeline_model_pr_review is None
        assert s.pipeline_model_follow_ups is None


class TestApplyOverrides:
    """`apply_overrides` runs the strict `_OVERRIDE_HANDLERS` table.

    Used by `SettingsManager.set_setting` and `Settings.load_from_file`.
    """

    def test_writes_per_stage_fields_from_nested_json(self):
        s = Settings()
        s.apply_overrides({
            "pipeline": {
                "models": {
                    "classify": "deepseek:deepseek-v3-small",
                    "implement": "anthropic:claude-sonnet-4-6",
                    "pr_review": "anthropic:claude-opus-4-8",
                }
            }
        })
        assert s.pipeline_model_classify == "deepseek:deepseek-v3-small"
        assert s.pipeline_model_implement == "anthropic:claude-sonnet-4-6"
        assert s.pipeline_model_pr_review == "anthropic:claude-opus-4-8"
        # Unset stages remain None (no implicit fallback at this layer).
        assert s.pipeline_model_refine is None
        assert s.pipeline_model_follow_ups is None

    def test_malformed_slug_is_skipped_with_warning(self, caplog):
        s = Settings()
        # Pre-set a value so we can prove the rejection doesn't clobber it.
        s.pipeline_model_implement = "deepseek:deepseek-v4-pro"
        with caplog.at_level("WARNING", logger="deile.config.settings"):
            s.apply_overrides({
                "pipeline": {"models": {"implement": "GARBAGE"}}
            })
        # Previous value preserved.
        assert s.pipeline_model_implement == "deepseek:deepseek-v4-pro"
        # And a warning was emitted (the strict path's safety net).
        assert any("pipeline.models.implement" in r.message
                   for r in caplog.records)


class TestNestedDictLoader:
    """`_apply_nested_dict` is the looser path used by
    ``_load_layered_settings``. It must accept ``pipeline.models.<stage>``
    keys so layered loading from ``~/.deile/settings.json`` works."""

    def test_per_stage_keys_round_trip(self):
        s = Settings()
        _apply_nested_dict(s, {
            "pipeline": {
                "models": {
                    "refine": "deepseek:deepseek-v4-pro",
                    "follow_ups": "deepseek:deepseek-v3-small",
                }
            }
        })
        assert s.pipeline_model_refine == "deepseek:deepseek-v4-pro"
        assert s.pipeline_model_follow_ups == "deepseek:deepseek-v3-small"


class TestEnvOverrides:
    """The DEILE_PIPELINE_MODEL_<STAGE> env vars are a convenience for ops
    (e.g. one-off override without editing settings.json). They use the same
    strict validator as the JSON path."""

    def test_env_vars_apply(self, monkeypatch):
        s = Settings()
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT", "anthropic:claude-opus-4-8")
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_PR_REVIEW", "deepseek:deepseek-v4-pro")
        _apply_env_overrides(s)
        assert s.pipeline_model_implement == "anthropic:claude-opus-4-8"
        assert s.pipeline_model_pr_review == "deepseek:deepseek-v4-pro"

    def test_env_var_with_malformed_slug_is_dropped(self, monkeypatch):
        s = Settings()
        s.pipeline_model_implement = "deepseek:deepseek-v4-pro"
        monkeypatch.setenv("DEILE_PIPELINE_MODEL_IMPLEMENT", "GARBAGE")
        # _apply_env_overrides swallows ValueError per the table's contract;
        # the previous value MUST survive (regression guard against a typo
        # in an env var silently clobbering a working slug).
        _apply_env_overrides(s)
        assert s.pipeline_model_implement == "deepseek:deepseek-v4-pro"
