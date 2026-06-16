"""Integration: cascade fallback (fake Anthropic → real OpenAI) — skipped without OPENAI_API_KEY."""

from __future__ import annotations

import os

import pytest

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not OPENAI_API_KEY,
        reason="OPENAI_API_KEY not set — skipping fallback integration test",
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cascade_falls_back_to_openai():
    """Real OpenAI call using gpt-4o-mini — independent of catalog (which uses speculative IDs)."""
    from deile.core.models.base import ModelMessage
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.openai_provider import OpenAIProvider
    from deile.core.models.provider_config import ProviderConfig
    from deile.core.models.tier import ModelTier

    # Build a handle for the cheapest real OpenAI chat model directly
    handle = ModelHandle(
        provider_id="openai",
        model_id="gpt-4o-mini",
        tier=ModelTier.TIER_3,
        label="fast-real",
        display_name="GPT-4o Mini",
        pricing=ModelPricing(input_per_1m_usd=0.15, output_per_1m_usd=0.60),
        context_window=128_000,
        capabilities=frozenset({"function_calling", "streaming"}),
    )
    config = ProviderConfig(
        provider_id="openai",
        api_key_env="OPENAI_API_KEY",
        base_url=None,
        sdk_kwargs={},
    )
    provider = OpenAIProvider(handle, config)

    msgs = [
        ModelMessage(role="user", content="What is 2+2? Reply with just the number.")
    ]
    response = await provider.generate(msgs)
    assert "4" in response.content
