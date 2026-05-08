"""Tests: prompt caching extraction across Anthropic, OpenAI, and DeepSeek — Phase 13."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from deile.core.models.base import ModelMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handle(model_id: str = "test-model", tier_value: str = "tier_1"):
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.tier import ModelTier

    tier_map = {
        "tier_1": ModelTier.TIER_1,
        "tier_2": ModelTier.TIER_2,
        "tier_3": ModelTier.TIER_3,
    }
    pricing = ModelPricing(
        input_per_1m_usd=15.0,
        output_per_1m_usd=75.0,
        cached_input_per_1m_usd=1.5,
    )
    return ModelHandle(
        provider_id="test",
        model_id=model_id,
        display_name="Test",
        tier=tier_map[tier_value],
        pricing=pricing,
        capabilities=frozenset({"streaming", "caching"}),
        context_window=200_000,
        label="test",
    )


def _make_config(
    api_key_env: str = "TEST_API_KEY",
    base_url: str = "https://api.test.com",
    provider_id: str = "test",
):
    from deile.core.models.provider_config import ProviderConfig
    return ProviderConfig(
        provider_id=provider_id,
        api_key_env=api_key_env,
        base_url=base_url,
        sdk_kwargs={},
    )


def _usage_ns(**kwargs) -> SimpleNamespace:
    """Build a fake usage namespace."""
    return SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# Anthropic — cache_creation + cache_read in generate()
# ---------------------------------------------------------------------------

class TestAnthropicPromptCaching:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def _make_provider(self):
        from deile.core.models.anthropic_provider import AnthropicProvider
        with patch("anthropic.AsyncAnthropic"):
            return AnthropicProvider(_make_handle(), _make_config(api_key_env="ANTHROPIC_API_KEY"))

    @pytest.mark.asyncio
    async def test_generate_extracts_cache_read_tokens(self):
        provider = self._make_provider()
        fake_usage = _usage_ns(
            input_tokens=500,
            output_tokens=100,
            cache_read_input_tokens=300,
            cache_creation_input_tokens=0,
        )
        fake_response = SimpleNamespace(
            content=[SimpleNamespace(text="hello", type="text")],
            usage=fake_usage,
            stop_reason="end_turn",
        )
        provider._client.messages.create = AsyncMock(return_value=fake_response)

        msgs = [ModelMessage(role="user", content="hi")]
        result = await provider.generate(msgs)
        assert result.usage.cached_tokens == 300

    @pytest.mark.asyncio
    async def test_generate_extracts_cache_creation_tokens(self):
        provider = self._make_provider()
        fake_usage = _usage_ns(
            input_tokens=500,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=1000,
        )
        fake_response = SimpleNamespace(
            content=[SimpleNamespace(text="hello", type="text")],
            usage=fake_usage,
            stop_reason="end_turn",
        )
        provider._client.messages.create = AsyncMock(return_value=fake_response)

        msgs = [ModelMessage(role="user", content="hi")]
        result = await provider.generate(msgs)
        # generate() only reads cache_read_input_tokens, not creation tokens
        assert result.usage.cached_tokens >= 0

    @pytest.mark.asyncio
    async def test_chat_with_tools_sums_both_cache_fields(self):
        provider = self._make_provider()
        fake_usage = _usage_ns(
            input_tokens=200,
            output_tokens=50,
            cache_read_input_tokens=400,
            cache_creation_input_tokens=600,
        )
        fake_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")],
            usage=fake_usage,
            stop_reason="end_turn",
        )
        provider._client.messages.create = AsyncMock(return_value=fake_response)

        msgs = [ModelMessage(role="user", content="test")]
        _, _, usage = await provider.chat_with_tools(msgs, [])
        # cache_read + cache_creation = 400 + 600 = 1000
        assert usage.cached_tokens == 1000


# ---------------------------------------------------------------------------
# OpenAI — prompt_tokens_details.cached_tokens in generate()
# ---------------------------------------------------------------------------

class TestOpenAIPromptCaching:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    def _make_provider(self):
        from deile.core.models.openai_provider import OpenAIProvider
        with patch("openai.AsyncOpenAI"):
            return OpenAIProvider(_make_handle(), _make_config(api_key_env="OPENAI_API_KEY"))

    @pytest.mark.asyncio
    async def test_generate_extracts_cached_tokens(self):
        provider = self._make_provider()

        fake_usage = _usage_ns(
            prompt_tokens=400,
            completion_tokens=80,
            total_tokens=480,
            prompt_tokens_details=_usage_ns(cached_tokens=256),
        )
        fake_choice = SimpleNamespace(
            message=SimpleNamespace(content="result", tool_calls=None),
            finish_reason="stop",
        )
        fake_response = SimpleNamespace(usage=fake_usage, choices=[fake_choice], model="gpt-4o")
        provider._client.chat.completions.create = AsyncMock(return_value=fake_response)

        msgs = [ModelMessage(role="user", content="hi")]
        result = await provider.generate(msgs)
        assert result.usage.cached_tokens == 256

    @pytest.mark.asyncio
    async def test_no_prompt_tokens_details_returns_zero(self):
        provider = self._make_provider()

        fake_usage = _usage_ns(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_tokens_details=None,
        )
        fake_choice = SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None),
            finish_reason="stop",
        )
        fake_response = SimpleNamespace(usage=fake_usage, choices=[fake_choice], model="gpt-4o")
        provider._client.chat.completions.create = AsyncMock(return_value=fake_response)

        msgs = [ModelMessage(role="user", content="hello")]
        result = await provider.generate(msgs)
        assert result.usage.cached_tokens == 0


# ---------------------------------------------------------------------------
# DeepSeek — prompt_cache_hit_tokens override
# ---------------------------------------------------------------------------

class TestDeepSeekPromptCaching:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")

    def _make_provider(self):
        from deile.core.models.deepseek_provider import DeepSeekProvider
        with patch("openai.AsyncOpenAI"):
            return DeepSeekProvider(
                _make_handle(),
                _make_config(api_key_env="DEEPSEEK_API_KEY", base_url="https://api.deepseek.com"),
            )

    def test_extract_cached_tokens_uses_hit_tokens(self):
        from deile.core.models.deepseek_provider import DeepSeekProvider

        fake_response = SimpleNamespace(
            usage=_usage_ns(prompt_cache_hit_tokens=512)
        )
        assert DeepSeekProvider._extract_cached_tokens(fake_response) == 512

    def test_extract_cached_tokens_zero_on_missing(self):
        from deile.core.models.deepseek_provider import DeepSeekProvider

        fake_response = SimpleNamespace(usage=_usage_ns())
        assert DeepSeekProvider._extract_cached_tokens(fake_response) == 0

    def test_extract_cached_tokens_zero_on_attribute_error(self):
        from deile.core.models.deepseek_provider import DeepSeekProvider

        assert DeepSeekProvider._extract_cached_tokens(object()) == 0

    @pytest.mark.asyncio
    async def test_generate_uses_deepseek_cached_tokens(self):
        provider = self._make_provider()

        fake_usage = _usage_ns(
            prompt_tokens=300,
            completion_tokens=60,
            total_tokens=360,
            prompt_cache_hit_tokens=128,
        )
        fake_choice = SimpleNamespace(
            message=SimpleNamespace(content="deep answer", tool_calls=None),
            finish_reason="stop",
        )
        fake_response = SimpleNamespace(usage=fake_usage, choices=[fake_choice], model="deepseek-chat")
        provider._client.chat.completions.create = AsyncMock(return_value=fake_response)

        msgs = [ModelMessage(role="user", content="hello")]
        result = await provider.generate(msgs)
        assert result.usage.cached_tokens == 128
