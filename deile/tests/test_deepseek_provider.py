"""Tests: DeepSeekProvider — Phase 5 (OpenAI-compatible subclass)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.deepseek_provider import DeepSeekProvider
from deile.core.models.base import ModelMessage, ModelType
from deile.core.models.catalog import ModelHandle, ModelPricing
from deile.core.models.errors import ProviderInvocationError
from deile.core.models.provider_config import ProviderConfig
from deile.core.models.stream_events import StreamEventType
from deile.core.models.tier import ModelTier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def handle() -> ModelHandle:
    return ModelHandle(
        provider_id="deepseek",
        model_id="deepseek-chat",
        tier=ModelTier.TIER_2,
        pricing=ModelPricing(
            input_per_1m_usd=0.27,
            output_per_1m_usd=1.10,
            cached_input_per_1m_usd=0.07,
        ),
        context_window=64_000,
        capabilities=frozenset({"function_calling", "streaming"}),
        display_name="DeepSeek Chat",
        label="default",
    )


@pytest.fixture
def config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
        sdk_kwargs={},
    )


@pytest.fixture
def provider(handle, config, monkeypatch) -> DeepSeekProvider:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-key")
    with patch("openai.AsyncOpenAI"):
        p = DeepSeekProvider(handle, config)
    return p


# ---------------------------------------------------------------------------
# Helpers (mirror openai_provider tests)
# ---------------------------------------------------------------------------

def _usage(prompt=10, completion=20, cached=0):
    u = MagicMock()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    details = MagicMock()
    details.cached_tokens = cached
    u.prompt_tokens_details = details
    return u


def _response(content="", finish_reason="stop"):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = []
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    r = MagicMock()
    r.choices = [choice]
    r.usage = _usage()
    return r


# ---------------------------------------------------------------------------
# Identity tests
# ---------------------------------------------------------------------------

def test_provider_id(provider):
    assert provider.provider_id == "deepseek"


def test_provider_name(provider):
    assert provider.provider_name == "deepseek"


def test_supported_types_no_vision(provider):
    types = provider.supported_types
    assert ModelType.CHAT in types
    assert ModelType.CODE in types
    assert ModelType.VISION not in types


def test_tier(provider):
    assert provider.tier == ModelTier.TIER_2


def test_pricing(provider):
    assert provider.pricing.input_per_1m_usd == 0.27
    assert provider.pricing.output_per_1m_usd == 1.10


# ---------------------------------------------------------------------------
# Functional tests — inherited from OpenAIProvider
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_returns_model_response(provider):
    resp = _response(content="DeepSeek answer")
    provider._client.chat.completions.create = AsyncMock(return_value=resp)

    result = await provider.generate([ModelMessage(role="user", content="hi")])

    assert result.content == "DeepSeek answer"
    assert result.model_name == "deepseek-chat"


@pytest.mark.asyncio
async def test_generate_auth_error_raises_envelope(provider):
    import openai as _oai

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.headers = {}

    err = _oai.AuthenticationError(
        message="Invalid API key",
        response=mock_response,
        body={"error": {"type": "invalid_api_key", "message": "Invalid API key"}},
    )
    provider._client.chat.completions.create = AsyncMock(side_effect=err)

    with pytest.raises(ProviderInvocationError) as exc_info:
        await provider.generate([ModelMessage(role="user", content="hi")])

    envelope = exc_info.value.envelope
    assert envelope.provider_id == "deepseek"
    assert envelope.error_type == "auth"


@pytest.mark.asyncio
async def test_generate_stream_events(provider):
    ev1 = MagicMock()
    ev1.type = "content.delta"
    ev1.content = "Hello from DeepSeek"

    final_usage = MagicMock()
    final_usage.prompt_tokens = 5
    final_usage.completion_tokens = 8
    details = MagicMock()
    details.cached_tokens = 0
    final_usage.prompt_tokens_details = details

    final_completion = MagicMock()
    final_completion.usage = final_usage

    async def _aiter(self_):
        yield ev1

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_cm)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    stream_cm.__aiter__ = _aiter
    stream_cm.get_final_completion = AsyncMock(return_value=final_completion)

    provider._client.chat.completions.stream = MagicMock(return_value=stream_cm)

    events = []
    async for ev in provider.generate_stream([ModelMessage(role="user", content="hi")]):
        events.append(ev)

    text_events = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
    usage_events = [e for e in events if e.type == StreamEventType.USAGE_FINAL]

    assert len(text_events) == 1
    assert text_events[0].text == "Hello from DeepSeek"
    assert len(usage_events) == 1


def test_estimate_cost(provider):
    from deile.core.models.base import ModelUsage
    usage = ModelUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    cost = provider.estimate_cost(usage)
    # 1M input @ $0.27 + 1M output @ $1.10 = $1.37
    assert abs(cost - 1.37) < 1e-4
