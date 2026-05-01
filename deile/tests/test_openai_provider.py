"""Tests: OpenAIProvider — Phase 4 (all SDK calls mocked)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.openai_provider import OpenAIProvider
from deile.core.models.base import ModelMessage, ModelUsage
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
        provider_id="openai",
        model_id="gpt-4o",
        tier=ModelTier.TIER_1,
        pricing=ModelPricing(
            input_per_1m_usd=2.50,
            output_per_1m_usd=10.00,
            cached_input_per_1m_usd=1.25,
        ),
        context_window=128_000,
        capabilities=frozenset({"function_calling", "streaming", "caching", "vision"}),
        display_name="GPT-4o",
        label="flagship",
    )


@pytest.fixture
def config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="openai",
        api_key_env="OPENAI_API_KEY",
        base_url=None,
        sdk_kwargs={},
    )


@pytest.fixture
def provider(handle, config, monkeypatch) -> OpenAIProvider:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    with patch("openai.AsyncOpenAI"):
        p = OpenAIProvider(handle, config)
    return p


# ---------------------------------------------------------------------------
# Helpers to build mock OpenAI response objects
# ---------------------------------------------------------------------------

def _usage(prompt=10, completion=20, cached=0):
    u = MagicMock()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    details = MagicMock()
    details.cached_tokens = cached
    u.prompt_tokens_details = details
    return u


def _choice(content="", finish_reason="stop", tool_calls=None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    c = MagicMock()
    c.message = msg
    c.finish_reason = finish_reason
    return c


def _response(content="", finish_reason="stop", tool_calls=None, usage_kwargs=None):
    r = MagicMock()
    r.choices = [_choice(content=content, finish_reason=finish_reason, tool_calls=tool_calls)]
    r.usage = _usage(**(usage_kwargs or {}))
    return r


def _tool_call(call_id: str, name: str, arguments: dict):
    fn = MagicMock()
    fn.name = name
    fn.arguments = json.dumps(arguments)
    tc = MagicMock()
    tc.id = call_id
    tc.function = fn
    return tc


# ---------------------------------------------------------------------------
# Test: generate()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_returns_model_response(provider):
    resp = _response(content="Hello!", finish_reason="stop")
    provider._client.chat.completions.create = AsyncMock(return_value=resp)

    result = await provider.generate([ModelMessage(role="user", content="Hi")])

    assert result.content == "Hello!"
    assert result.model_name == "gpt-4o"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 20


@pytest.mark.asyncio
async def test_generate_system_instruction(provider):
    resp = _response(content="ok")
    mock_create = AsyncMock(return_value=resp)
    provider._client.chat.completions.create = mock_create

    await provider.generate(
        [ModelMessage(role="user", content="Hi")],
        system_instruction="You are a test assistant",
    )

    call_kwargs = mock_create.call_args.kwargs
    messages = call_kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a test assistant"


# ---------------------------------------------------------------------------
# Test: chat_with_tools() — 1-turn (no tool calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_with_tools_no_tool_calls(provider):
    resp = _response(content="Paris", finish_reason="stop")
    provider._client.chat.completions.create = AsyncMock(return_value=resp)

    text, tool_results, usage = await provider.chat_with_tools(
        messages=[ModelMessage(role="user", content="Capital of France?")],
        tools=[],
    )

    assert text == "Paris"
    assert tool_results == []
    assert usage.total_tokens == 30


# ---------------------------------------------------------------------------
# Test: chat_with_tools() — 1 tool call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_with_tools_one_tool_call(provider):
    tc = _tool_call("tc_1", "bash", {"command": "ls"})
    resp1 = _response(content="", finish_reason="tool_calls", tool_calls=[tc])
    resp2 = _response(content="files: main.py", finish_reason="stop")

    provider._client.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])

    from deile.tools.base import ToolResult, ToolStatus
    mock_result = ToolResult(status=ToolStatus.SUCCESS, data="main.py", message="ok")

    with patch.object(provider, "_execute_tool", AsyncMock(return_value=(mock_result, {"status": "success", "result": "main.py"}))):
        text, tool_results, usage = await provider.chat_with_tools(
            messages=[ModelMessage(role="user", content="List files")],
            tools=[],
        )

    assert "files: main.py" in text
    assert len(tool_results) == 1
    assert tool_results[0].is_success


# ---------------------------------------------------------------------------
# Test: chat_with_tools() — 2 sequential iterations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_with_tools_two_iterations(provider):
    tc1 = _tool_call("tc_1", "bash", {"command": "ls"})
    tc2 = _tool_call("tc_2", "bash", {"command": "cat README.md"})
    resp1 = _response(content="", finish_reason="tool_calls", tool_calls=[tc1])
    resp2 = _response(content="", finish_reason="tool_calls", tool_calls=[tc2])
    resp3 = _response(content="done", finish_reason="stop")

    provider._client.chat.completions.create = AsyncMock(side_effect=[resp1, resp2, resp3])

    from deile.tools.base import ToolResult, ToolStatus
    mock_tr = ToolResult(status=ToolStatus.SUCCESS, data="ok")

    with patch.object(provider, "_execute_tool", AsyncMock(return_value=(mock_tr, {"status": "success", "result": "ok"}))):
        text, tool_results, usage = await provider.chat_with_tools(
            messages=[ModelMessage(role="user", content="Do stuff")],
            tools=[],
        )

    assert text == "done"
    assert len(tool_results) == 2


# ---------------------------------------------------------------------------
# Test: auth error → ProviderInvocationError with envelope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_auth_error_raises_envelope(provider):
    import openai as _oai

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.headers = {}
    mock_request = MagicMock()

    err = _oai.AuthenticationError(
        message="Invalid API key",
        response=mock_response,
        body={"error": {"type": "invalid_api_key", "message": "Invalid API key"}},
    )
    provider._client.chat.completions.create = AsyncMock(side_effect=err)

    with pytest.raises(ProviderInvocationError) as exc_info:
        await provider.generate([ModelMessage(role="user", content="hi")])

    envelope = exc_info.value.envelope
    assert envelope.provider_id == "openai"
    assert envelope.error_type == "auth"
    assert envelope.http_status == 401
    assert isinstance(envelope.raw_json, dict)


# ---------------------------------------------------------------------------
# Test: _extract_cached_tokens
# ---------------------------------------------------------------------------

def test_extract_cached_tokens_present(provider):
    resp = _response(usage_kwargs={"cached": 500})
    result = provider._extract_cached_tokens(resp)
    assert result == 500


def test_extract_cached_tokens_missing(provider):
    resp = MagicMock()
    resp.usage = MagicMock(spec=[])  # no attributes
    result = provider._extract_cached_tokens(resp)
    assert result == 0


# ---------------------------------------------------------------------------
# Test: streaming — yields TEXT_DELTA + USAGE_FINAL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_stream_events(provider):
    """generate_stream yields TEXT_DELTA events and ends with USAGE_FINAL.

    The provider now drives streaming via ``await chat.completions.create(stream=True)``
    which returns an async iterator of ChatCompletionChunk objects (each with a
    ``choices[0].delta`` and an optional ``usage``).
    """
    from types import SimpleNamespace

    def _text_chunk(text: str):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=text, tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        )

    def _final_chunk(prompt=5, completion=10):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=None, finish_reason="stop")],
            usage=SimpleNamespace(
                prompt_tokens=prompt,
                completion_tokens=completion,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )

    chunks = [_text_chunk("Hello"), _text_chunk(" World"), _final_chunk()]

    async def _replay(_chunks):
        for c in _chunks:
            yield c

    async def _fake_create(**kw):
        return _replay(chunks)

    provider._client.chat.completions.create = AsyncMock(side_effect=_fake_create)

    events = []
    async for ev in provider.generate_stream([ModelMessage(role="user", content="hi")]):
        events.append(ev)

    text_events = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
    usage_events = [e for e in events if e.type == StreamEventType.USAGE_FINAL]

    assert len(text_events) == 2
    assert text_events[0].text == "Hello"
    assert text_events[1].text == " World"
    assert len(usage_events) == 1
    assert usage_events[0].usage.input_tokens == 5
    assert usage_events[0].usage.output_tokens == 10


# ---------------------------------------------------------------------------
# Test: cost estimation
# ---------------------------------------------------------------------------

def test_estimate_cost(provider):
    usage = ModelUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    cost = provider.estimate_cost(usage)
    # 1M input @ $2.50 + 1M output @ $10.00 = $12.50
    assert abs(cost - 12.50) < 1e-4


# ---------------------------------------------------------------------------
# Test: provider_id and tier
# ---------------------------------------------------------------------------

def test_provider_id(provider):
    assert provider.provider_id == "openai"


def test_tier(provider, handle):
    assert provider.tier == ModelTier.TIER_1


def test_pricing(provider, handle):
    assert provider.pricing.input_per_1m_usd == 2.50
    assert provider.pricing.output_per_1m_usd == 10.00
