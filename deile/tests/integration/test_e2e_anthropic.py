"""Integration: real Anthropic API call — skipped if ANTHROPIC_API_KEY absent."""

from __future__ import annotations

import os

import pytest

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
pytestmark = pytest.mark.skipif(
    not ANTHROPIC_API_KEY,
    reason="ANTHROPIC_API_KEY not set — skipping real API test",
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anthropic_simple_generate():
    from deile.core.models.catalog import ModelCatalog
    from deile.core.models.anthropic_provider import AnthropicProvider
    from deile.core.models.base import ModelMessage
    from deile.core.models.provider_config import ProviderConfig
    from pathlib import Path

    yaml_path = Path(__file__).parents[3] / "deile" / "config" / "model_providers.yaml"
    catalog = ModelCatalog.from_yaml(yaml_path)
    handle = catalog.get("anthropic", "claude-haiku-4-5")
    config = ProviderConfig(
        provider_id="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        base_url=None,
        sdk_kwargs={},
    )
    provider = AnthropicProvider(handle, config)

    msgs = [ModelMessage(role="user", content="What is 2+2? Reply with just the number.")]
    response = await provider.generate(msgs)
    assert "4" in response.content
    assert response.usage.prompt_tokens > 0
