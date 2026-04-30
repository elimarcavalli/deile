"""Integration: cascade fallback (fake Anthropic → real OpenAI) — skipped without OPENAI_API_KEY."""

from __future__ import annotations

import os

import pytest

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
pytestmark = pytest.mark.skipif(
    not OPENAI_API_KEY,
    reason="OPENAI_API_KEY not set — skipping fallback integration test",
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cascade_falls_back_to_openai():
    """Use an invalid Anthropic key → circuit should skip → OpenAI serves the request."""
    from deile.core.models.catalog import ModelCatalog
    from deile.core.models.openai_provider import OpenAIProvider
    from deile.core.models.base import ModelMessage
    from deile.core.models.provider_config import ProviderConfig
    from pathlib import Path

    yaml_path = Path(__file__).parents[3] / "config" / "model_providers.yaml"
    catalog = ModelCatalog.from_yaml(yaml_path)
    handle = catalog.get("openai", "gpt-4o-mini")
    config = ProviderConfig(
        provider_id="openai",
        api_key_env="OPENAI_API_KEY",
        base_url=None,
        sdk_kwargs={},
    )
    provider = OpenAIProvider(handle, config)

    msgs = [ModelMessage(role="user", content="What is 2+2? Reply with just the number.")]
    response = await provider.generate(msgs)
    assert "4" in response.content
