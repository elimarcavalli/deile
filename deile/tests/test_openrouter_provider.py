"""Tests: OpenRouterProvider — OpenAI-compatible gateway subclass (OR1/OR5).

Covers: identity, base_url/headers wiring (OR1), authoritative cost from the
response's ``usage.cost`` with catalog fallback (OR5), the ``usage.include``
request flag, cached-token extraction, and circuit-breaker reuse (Part-3c).
All HTTP is mocked — no real OpenRouter call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.base import ModelMessage, ModelType, ModelUsage
from deile.core.models.catalog import ModelHandle, ModelPricing
from deile.core.models.errors import ProviderInvocationError
from deile.core.models.openrouter_provider import OpenRouterProvider
from deile.core.models.provider_config import ProviderConfig
from deile.core.models.stream_events import StreamEventType
from deile.core.models.tier import ModelTier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handle() -> ModelHandle:
    # model_id carries a '/' — exercises the slug/key path that the regex now allows.
    return ModelHandle(
        provider_id="openrouter",
        model_id="anthropic/claude-sonnet-4.6",
        tier=ModelTier.TIER_2,
        pricing=ModelPricing(input_per_1m_usd=3.0, output_per_1m_usd=15.0),
        context_window=1_000_000,
        capabilities=frozenset({"function_calling", "streaming"}),
        display_name="Claude Sonnet 4.6 (OpenRouter)",
        label="premium",
    )


@pytest.fixture
def config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        sdk_kwargs={
            "default_headers": {"HTTP-Referer": "https://x", "X-Title": "DEILE"}
        },
    )


@pytest.fixture
def provider(handle, config, monkeypatch) -> OpenRouterProvider:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    with patch("openai.AsyncOpenAI"):
        p = OpenRouterProvider(handle, config)
    return p


def _usage(prompt=10, completion=20, cached=0, cost=None):
    u = MagicMock()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    details = MagicMock()
    details.cached_tokens = cached
    u.prompt_tokens_details = details
    # OpenRouter surfaces the billed cost on usage.cost when usage.include=true.
    u.cost = cost
    return u


def _response(content="", finish_reason="stop", cost=None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = []
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    r = MagicMock()
    r.choices = [choice]
    r.usage = _usage(cost=cost)
    return r


# ---------------------------------------------------------------------------
# OR1 — identity + adapter wiring
# ---------------------------------------------------------------------------


def test_provider_id(provider):
    assert provider.provider_id == "openrouter"


def test_provider_name(provider):
    assert provider.provider_name == "openrouter"


def test_supported_types(provider):
    assert ModelType.CHAT in provider.supported_types


def test_model_name_keeps_slash(provider):
    # The '/' in the upstream vendor id must survive into the wire model field.
    assert provider.model_name == "anthropic/claude-sonnet-4.6"


def test_adapter_wires_base_url_and_headers(handle, config, monkeypatch):
    """OR1: the OpenAI adapter must forward base_url + default_headers from
    ProviderConfig — no hardcoded endpoint/key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    with patch("openai.AsyncOpenAI") as mock_client:
        OpenRouterProvider(handle, config)
    _, kwargs = mock_client.call_args
    assert kwargs["api_key"] == "sk-or-test-key"
    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert kwargs["default_headers"]["X-Title"] == "DEILE"


def test_missing_key_raises(handle, config, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch("openai.AsyncOpenAI"):
        with pytest.raises(ValueError):
            OpenRouterProvider(handle, config)


# ---------------------------------------------------------------------------
# OR1 — usage.include request flag
# ---------------------------------------------------------------------------


def test_provider_extra_body_requests_usage_cost(provider):
    assert provider._provider_extra_body() == {"usage": {"include": True}}


@pytest.mark.asyncio
async def test_generate_passes_usage_include(provider):
    create = AsyncMock(return_value=_response(content="hi", cost=0.0012))
    provider._client.chat.completions.create = create
    await provider.generate([ModelMessage(role="user", content="hi")])
    _, kwargs = create.call_args
    assert kwargs["extra_body"]["usage"]["include"] is True


# ---------------------------------------------------------------------------
# OR5 — authoritative reported cost vs catalog fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_uses_reported_cost(provider):
    """When OpenRouter reports usage.cost, it is authoritative (NOT the catalog)."""
    create = AsyncMock(return_value=_response(content="hi", cost=0.0042))
    provider._client.chat.completions.create = create
    resp = await provider.generate([ModelMessage(role="user", content="hi")])
    assert resp.usage.cost_estimate == pytest.approx(0.0042)


@pytest.mark.asyncio
async def test_generate_falls_back_to_catalog_when_no_cost(provider):
    """No usage.cost → estimate from the catalog pricing (no silent zero)."""
    create = AsyncMock(return_value=_response(content="hi", cost=None))
    # 10 prompt @ $3/M + 20 completion @ $15/M
    provider._client.chat.completions.create = create
    resp = await provider.generate([ModelMessage(role="user", content="hi")])
    expected = (10 / 1_000_000) * 3.0 + (20 / 1_000_000) * 15.0
    assert resp.usage.cost_estimate == pytest.approx(expected)


def test_estimate_cost_prefers_reported():
    usage = ModelUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    usage.extra["reported_cost_usd"] = 0.5
    handle = ModelHandle(
        provider_id="openrouter",
        model_id="deepseek/deepseek-chat",
        tier=ModelTier.TIER_3,
        pricing=ModelPricing(input_per_1m_usd=0.28, output_per_1m_usd=0.88),
        context_window=163_840,
        capabilities=frozenset(),
        display_name="x",
        label="x",
    )
    cfg = ProviderConfig(
        provider_id="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )
    import os

    os.environ["OPENROUTER_API_KEY"] = "sk-or-test-key"
    with patch("openai.AsyncOpenAI"):
        p = OpenRouterProvider(handle, cfg)
    # reported cost wins over the catalog ($1.16 it would otherwise be).
    assert p.estimate_cost(usage) == pytest.approx(0.5)


def test_reported_cost_from_handles_response_and_usage(provider):
    resp = _response(cost=0.01)
    assert provider._reported_cost_from(resp) == pytest.approx(0.01)
    # streaming path passes a bare usage object (no nested .usage attr) — mirror
    # the real SimpleNamespace chunk.usage shape, not a MagicMock (which would
    # auto-fabricate a .usage child).
    bare = SimpleNamespace(cost=0.02)
    assert provider._reported_cost_from(bare) == pytest.approx(0.02)
    # absent → None (fallback)
    assert provider._reported_cost_from(SimpleNamespace(cost=None)) is None


@pytest.mark.asyncio
async def test_stream_uses_reported_cost(provider):
    text = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="hello", tool_calls=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    final = SimpleNamespace(
        choices=[SimpleNamespace(delta=None, finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=8,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            cost=0.009,
        ),
    )

    async def _replay(chunks):
        for c in chunks:
            yield c

    provider._client.chat.completions.create = AsyncMock(
        side_effect=lambda **kw: _replay([text, final])
    )
    usage_events = [
        ev
        async for ev in provider.generate_stream(
            [ModelMessage(role="user", content="hi")]
        )
        if ev.type == StreamEventType.USAGE_FINAL
    ]
    assert len(usage_events) == 1
    assert usage_events[0].usage.cost_usd == pytest.approx(0.009)


# ---------------------------------------------------------------------------
# Part 3c — circuit breaker / error envelope keyed by provider_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_error_maps_to_envelope(provider):
    import openai as _oai

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {}
    err = _oai.RateLimitError(
        message="rate limited",
        response=mock_response,
        body={"error": {"type": "rate_limit_exceeded", "message": "slow down"}},
    )
    provider._client.chat.completions.create = AsyncMock(side_effect=err)
    with pytest.raises(ProviderInvocationError) as exc_info:
        await provider.generate([ModelMessage(role="user", content="hi")])
    assert exc_info.value.envelope.provider_id == "openrouter"


# ---------------------------------------------------------------------------
# OR3 — conditional bootstrap registration
# ---------------------------------------------------------------------------


def test_bootstrap_registers_openrouter_when_key_present(monkeypatch):
    from pathlib import Path

    from deile.core.models.bootstrap import bootstrap_providers
    from deile.core.models.tier_router import reset_tier_router

    yaml_path = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"
    for k in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    reset_tier_router()

    with patch("deile.core.models.bootstrap._import_provider_class") as mk:
        mk.return_value = MagicMock(return_value=MagicMock())
        registered = bootstrap_providers(yaml_path=yaml_path)
    assert "openrouter" in registered


def test_bootstrap_skips_openrouter_without_key(monkeypatch):
    from pathlib import Path

    from deile.core.models.bootstrap import bootstrap_providers

    yaml_path = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"
    for k in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    registered = bootstrap_providers(yaml_path=yaml_path)
    assert "openrouter" not in registered


def test_bootstrap_maps_openrouter_class():
    from deile.core.models.bootstrap import _PROVIDER_CLASSES

    assert _PROVIDER_CLASSES["openrouter"].endswith("OpenRouterProvider")


def test_tier_router_breaker_covers_openrouter():
    """The TierRouter circuit breaker is keyed by provider_id — a string — so a
    provider registered as 'openrouter' is covered without breaker changes."""
    from deile.core.models.tier_router import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    assert cb.allow_request("openrouter") is True
    cb.record_failure("openrouter")
    cb.record_failure("openrouter")
    assert cb.is_open("openrouter") is True


# ---------------------------------------------------------------------------
# OR7 — E2E (mocked HTTP): provider → UsageRepository records the reported cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_chat_records_reported_cost_in_usage_repo(provider):
    """End-to-end with HTTP mocked: a no-tool chat_with_tools turn flows the
    OpenRouter-reported ``usage.cost`` all the way into UsageRepository's
    ``cost_usd`` — proving no undercount and no silent zero (OR5/OR7)."""
    create = AsyncMock(return_value=_response(content="done", cost=0.0031))
    provider._client.chat.completions.create = create

    recorded = {}

    async def _fake_record_from_provider(**kw):
        recorded.update(kw)

    fake_repo = MagicMock()
    fake_repo.record_from_provider = AsyncMock(side_effect=_fake_record_from_provider)

    with patch(
        "deile.storage.usage_repository.get_usage_repository", return_value=fake_repo
    ):
        text, tool_results, usage = await provider.chat_with_tools(
            [ModelMessage(role="user", content="hi")],
            tools=[],
        )

    assert text == "done"
    # the reported cost won — not the catalog estimate
    assert usage.cost_estimate == pytest.approx(0.0031)
    fake_repo.record_from_provider.assert_awaited_once()
    assert recorded["provider_id"] == "openrouter"
    assert recorded["usage"].cost_estimate == pytest.approx(0.0031)
