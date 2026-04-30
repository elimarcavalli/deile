"""Tests: BaseProvider (ModelProvider) unified contract — Phase 1."""

from __future__ import annotations

import pytest
from typing import Any, AsyncIterator, List, Optional

from deile.core.models.base import (
    ModelMessage,
    ModelProvider,
    ModelResponse,
    ModelSize,
    ModelTier,
    ModelType,
    ModelUsage,
    tier_to_model_size,
)
from deile.core.models.catalog import ModelPricing
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent


# ---------------------------------------------------------------------------
# Minimal concrete provider used across all tests
# ---------------------------------------------------------------------------

class _MockProvider(ModelProvider):
    provider_name = "mock"
    provider_id = "mock"

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT]

    @property
    def model_size(self) -> ModelSize:
        return ModelSize.MEDIUM

    async def generate(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> ModelResponse:
        return ModelResponse(
            content="hello",
            model_name=self.model_name,
            usage=ModelUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )

    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        yield UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hello")
        yield UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL)


class _PricedProvider(_MockProvider):
    """Mock provider that exposes pricing."""

    @property
    def pricing(self) -> Optional[ModelPricing]:
        return ModelPricing(
            input_per_1m_usd=3.00,
            output_per_1m_usd=15.00,
            cached_input_per_1m_usd=0.30,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    return _MockProvider(model_name="mock-model")


@pytest.fixture
def priced():
    return _PricedProvider(model_name="priced-model")


def test_provider_id_defaults_to_provider_name(provider):
    assert provider.provider_id == "mock"


def test_tier_defaults_from_model_size(provider):
    # ModelSize.MEDIUM → ModelTier.TIER_2
    assert provider.tier == ModelTier.TIER_2


def test_pricing_defaults_to_none(provider):
    assert provider.pricing is None


@pytest.mark.asyncio
async def test_generate_returns_response(provider):
    msgs = [ModelMessage(role="user", content="hi")]
    response = await provider.generate(msgs)
    assert response.content == "hello"
    assert response.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_generate_stream_yields_events(provider):
    msgs = [ModelMessage(role="user", content="hi")]
    events = [e async for e in provider.generate_stream(msgs)]
    types = [e.type for e in events]
    assert StreamEventType.TEXT_DELTA in types


@pytest.mark.asyncio
async def test_chat_with_tools_default_fallback(provider):
    msgs = [ModelMessage(role="user", content="hi")]
    text, tool_results, usage = await provider.chat_with_tools(msgs, tools=[])
    assert text == "hello"
    assert tool_results == []
    assert isinstance(usage, ModelUsage)


def test_estimate_cost_no_pricing(provider):
    usage = ModelUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    assert provider.estimate_cost(usage) == 0.0


def test_estimate_cost_with_pricing(priced):
    usage = ModelUsage(
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
        cached_tokens=0,
    )
    cost = priced.estimate_cost(usage)
    # 1M input @ $3.00 + 1M output @ $15.00 = $18.00
    assert abs(cost - 18.00) < 1e-6


def test_estimate_cost_with_cached_tokens(priced):
    usage = ModelUsage(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=1_000_000,
    )
    cost = priced.estimate_cost(usage)
    # 1M cached @ $0.30
    assert abs(cost - 0.30) < 1e-6


def test_tier_to_model_size_mapping():
    assert tier_to_model_size(ModelTier.TIER_1) == ModelSize.LARGE
    assert tier_to_model_size(ModelTier.TIER_2) == ModelSize.MEDIUM
    assert tier_to_model_size(ModelTier.TIER_3) == ModelSize.SMALL
    assert tier_to_model_size(ModelTier.TIER_4) == ModelSize.SMALL


def test_model_usage_has_cached_tokens_field():
    u = ModelUsage(cached_tokens=42)
    assert u.cached_tokens == 42
