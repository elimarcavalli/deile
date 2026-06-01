"""Tests for the reasoning-effort vocabulary + per-provider mapping.

Single source of truth: ``deile.core.models.reasoning``. Covers the public
contract that the panel, settings validator, worker servers and providers all
depend on:

- ``valid_efforts_for`` returns the right level set per (worker, provider).
- ``request_overrides`` maps a coarse level to the native request param for
  anthropic / openai / deepseek (via extra_body), and is a no-op for gemini.
- ``gemini_thinking_kwargs`` picks ``thinking_level`` (3.x) vs
  ``thinking_budget`` (2.5) and clamps per model.
- Unknown / auto levels collapse to "no override" (fail-open).
- ``resolve_session_reasoning`` precedence: context_data > settings > None.
"""

from __future__ import annotations

import pytest

from deile.core.models import reasoning as R


@pytest.mark.unit
class TestVocabulary:
    def test_claude_code_efforts_are_the_user_spec(self):
        assert R.CLAUDE_CODE_EFFORTS == (
            "low", "medium", "high", "xhigh", "max", "ultracode", "auto",
        )

    def test_valid_efforts_claude_worker_always_claude_vocab(self):
        assert R.valid_efforts_for(worker="claude-worker", provider_id="anthropic") == R.CLAUDE_CODE_EFFORTS
        # claude-worker is anthropic-only; provider_id is ignored for it.
        assert R.valid_efforts_for(worker="claude-worker", provider_id=None) == R.CLAUDE_CODE_EFFORTS

    def test_valid_efforts_deile_worker_by_provider(self):
        assert R.valid_efforts_for(worker="deile-worker", provider_id="anthropic") == R.CLAUDE_CODE_EFFORTS
        assert R.valid_efforts_for(worker="deile-worker", provider_id="openai") == R.OPENAI_EFFORTS
        assert R.valid_efforts_for(worker="deile-worker", provider_id="gemini") == R.GEMINI_EFFORTS
        assert R.valid_efforts_for(worker="deile-worker", provider_id="deepseek") == R.DEEPSEEK_EFFORTS

    def test_unknown_provider_falls_back_to_claude_vocab(self):
        assert R.valid_efforts_for(worker="deile-worker", provider_id="weird") == R.CLAUDE_CODE_EFFORTS

    def test_is_valid_effort(self):
        assert R.is_valid_effort("XHigh") is True  # case-insensitive
        assert R.is_valid_effort("ultracode") is True
        assert R.is_valid_effort("off") is True
        assert R.is_valid_effort("bogus") is False
        assert R.is_valid_effort(None) is False
        assert R.is_valid_effort(123) is False

    def test_normalize(self):
        assert R.normalize_effort("  HIGH ") == "high"
        assert R.normalize_effort("") is None
        assert R.normalize_effort(None) is None


@pytest.mark.unit
class TestAnthropicMapping:
    def test_opus_supports_all_levels(self):
        assert R.request_overrides("anthropic", "claude-opus-4-8", "xhigh") == {"output_config": {"effort": "xhigh"}}
        assert R.request_overrides("anthropic", "claude-opus-4-8", "max") == {"output_config": {"effort": "max"}}

    def test_sonnet_has_no_xhigh_falls_back_to_max(self):
        assert R.request_overrides("anthropic", "claude-sonnet-4-6", "xhigh") == {"output_config": {"effort": "max"}}
        assert R.request_overrides("anthropic", "claude-sonnet-4-6", "high") == {"output_config": {"effort": "high"}}

    def test_ultracode_maps_to_max(self):
        assert R.request_overrides("anthropic", "claude-opus-4-8", "ultracode") == {"output_config": {"effort": "max"}}

    def test_auto_omits(self):
        assert R.request_overrides("anthropic", "claude-opus-4-8", "auto") == {}

    def test_haiku_has_no_effort_param(self):
        assert R.request_overrides("anthropic", "claude-haiku-4-5", "high") == {}


@pytest.mark.unit
class TestOpenAIMapping:
    def test_levels_passthrough(self):
        assert R.request_overrides("openai", "gpt-5.5", "medium") == {"reasoning_effort": "medium"}
        assert R.request_overrides("openai", "gpt-5.4", "none") == {"reasoning_effort": "none"}

    def test_max_ultracode_map_to_xhigh(self):
        assert R.request_overrides("openai", "gpt-5.5", "max") == {"reasoning_effort": "xhigh"}
        assert R.request_overrides("openai", "gpt-5.5", "ultracode") == {"reasoning_effort": "xhigh"}

    def test_nano_omits(self):
        assert R.request_overrides("openai", "gpt-5.4-nano", "high") == {}

    def test_auto_omits(self):
        assert R.request_overrides("openai", "gpt-5.5", "auto") == {}


@pytest.mark.unit
class TestDeepSeekMapping:
    def test_off_disables_thinking(self):
        assert R.request_overrides("deepseek", "deepseek-v4-pro", "off") == {"thinking": {"type": "disabled"}}

    def test_high_and_max(self):
        assert R.request_overrides("deepseek", "deepseek-v4-pro", "high") == {"reasoning_effort": "high"}
        assert R.request_overrides("deepseek", "deepseek-v4-flash", "max") == {"reasoning_effort": "max"}
        assert R.request_overrides("deepseek", "deepseek-v4-pro", "ultracode") == {"reasoning_effort": "max"}

    def test_low_medium_alias_to_high(self):
        assert R.request_overrides("deepseek", "deepseek-v4-pro", "low") == {"reasoning_effort": "high"}

    def test_auto_omits(self):
        assert R.request_overrides("deepseek", "deepseek-v4-pro", "auto") == {}


@pytest.mark.unit
class TestGeminiMapping:
    def test_request_overrides_is_noop_for_gemini(self):
        # gemini uses gemini_thinking_kwargs, not extra_body.
        assert R.request_overrides("gemini", "gemini-3.5-flash", "high") == {}

    def test_3x_uses_thinking_level(self):
        assert R.gemini_thinking_kwargs("gemini-3.5-flash", "medium") == {"thinking_level": "medium"}
        assert R.gemini_thinking_kwargs("gemini-3.1-flash-lite", "off") == {"thinking_level": "minimal"}
        # xhigh/max collapse to high for 3.x
        assert R.gemini_thinking_kwargs("gemini-3.1-pro-preview", "max") == {"thinking_level": "high"}

    def test_25_uses_thinking_budget(self):
        assert R.gemini_thinking_kwargs("gemini-2.5-flash", "off") == {"thinking_budget": 0}
        assert R.gemini_thinking_kwargs("gemini-2.5-flash", "low") == {"thinking_budget": 1024}
        assert R.gemini_thinking_kwargs("gemini-2.5-flash", "high") == {"thinking_budget": 24576}

    def test_25_pro_cannot_disable(self):
        # 2.5-pro min budget is 128; "off" clamps up.
        assert R.gemini_thinking_kwargs("gemini-2.5-pro", "off") == {"thinking_budget": 128}

    def test_auto_omits(self):
        assert R.gemini_thinking_kwargs("gemini-3.5-flash", "auto") is None
        assert R.gemini_thinking_kwargs("gemini-2.5-pro", None) is None


@pytest.mark.unit
class TestSessionResolver:
    def test_context_data_wins(self, monkeypatch):
        from deile.config.settings import get_settings, reset_settings
        monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
        reset_settings()
        get_settings().reasoning_effort = "low"

        class _S:
            context_data = {"reasoning_effort": "max"}

        assert R.resolve_session_reasoning(_S()) == "max"
        reset_settings()

    def test_falls_back_to_settings(self, monkeypatch):
        from deile.config.settings import get_settings, reset_settings
        monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
        reset_settings()
        get_settings().reasoning_effort = "high"

        class _S:
            context_data = {}

        assert R.resolve_session_reasoning(_S()) == "high"
        reset_settings()

    def test_none_when_unset(self, monkeypatch):
        from deile.config.settings import reset_settings
        monkeypatch.delenv("DEILE_REASONING_EFFORT", raising=False)
        reset_settings()

        class _S:
            context_data = {}

        assert R.resolve_session_reasoning(_S()) is None
        reset_settings()
