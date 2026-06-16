"""Tests: AnthropicProvider — Phase 3 (all SDK calls mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.anthropic_provider import AnthropicProvider
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
        provider_id="anthropic",
        model_id="claude-opus-4-8",
        tier=ModelTier.TIER_1,
        pricing=ModelPricing(
            input_per_1m_usd=5.00,
            output_per_1m_usd=25.00,
            cached_input_per_1m_usd=0.50,
        ),
        context_window=200_000,
        capabilities=frozenset({"function_calling", "streaming", "caching", "vision"}),
        display_name="Claude Opus 4.8",
        label="flagship",
    )


@pytest.fixture
def config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        base_url=None,
        sdk_kwargs={"default_headers": {"anthropic-beta": "prompt-caching-2024-07-31"}},
    )


@pytest.fixture
def provider(handle, config, monkeypatch) -> AnthropicProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    with patch("anthropic.AsyncAnthropic"):
        p = AnthropicProvider(handle, config)
    return p


# ---------------------------------------------------------------------------
# Helpers to build mock Anthropic response objects
# ---------------------------------------------------------------------------


def _usage(inp=10, out=20, cached_read=0, cached_create=0):
    u = MagicMock()
    u.input_tokens = inp
    u.output_tokens = out
    u.cache_read_input_tokens = cached_read
    u.cache_creation_input_tokens = cached_create
    return u


def _text_block(text: str):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(tool_id: str, name: str, input_: dict):
    b = MagicMock()
    b.type = "tool_use"
    b.id = tool_id
    b.name = name
    b.input = input_
    return b


def _response(content, stop_reason="end_turn", usage_kwargs=None):
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    r.usage = _usage(**(usage_kwargs or {}))
    return r


# ---------------------------------------------------------------------------
# Test: generate()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_returns_model_response(provider):
    resp = _response([_text_block("Hello!")], stop_reason="end_turn")
    provider._client.messages.create = AsyncMock(return_value=resp)

    result = await provider.generate([ModelMessage(role="user", content="Hi")])

    assert result.content == "Hello!"
    assert result.model_name == "claude-opus-4-8"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 20


@pytest.mark.asyncio
async def test_generate_system_instruction(provider):
    resp = _response([_text_block("ok")], stop_reason="end_turn")
    mock_create = AsyncMock(return_value=resp)
    provider._client.messages.create = mock_create

    await provider.generate(
        [ModelMessage(role="user", content="Hi")],
        system_instruction="You are a test assistant",
    )

    call_kwargs = mock_create.call_args.kwargs
    assert "system" in call_kwargs
    # system is a list of blocks with cache_control
    system_text = call_kwargs["system"][0]["text"]
    # Persona base preservada como prefixo (necessária para o cache anthropic).
    assert system_text.startswith("You are a test assistant")
    # Runtime-identity block apendado ao final para anti-alucinação.
    assert "<runtime_identity>" in system_text
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Test: chat_with_tools() — 1-turn (no tool calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_with_tools_no_tool_calls(provider):
    resp = _response([_text_block("Paris")], stop_reason="end_turn")
    provider._client.messages.create = AsyncMock(return_value=resp)

    text, tool_results, usage = await provider.chat_with_tools(
        messages=[ModelMessage(role="user", content="Capital of France?")],
        tools=[],
    )

    assert text == "Paris"
    assert tool_results == []
    assert usage.total_tokens == 30


# ---------------------------------------------------------------------------
# Test: chat_with_tools() — 2-turn with 1 tool call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_with_tools_one_tool_call(provider):
    tool_block = _tool_use_block("tu_1", "bash", {"command": "ls"})
    resp1 = _response([tool_block], stop_reason="tool_use")
    resp2 = _response([_text_block("files: main.py")], stop_reason="end_turn")

    call_sequence = [resp1, resp2]
    provider._client.messages.create = AsyncMock(side_effect=call_sequence)

    # Mock tool execution
    from deile.tools.base import ToolResult, ToolStatus

    mock_result = ToolResult(
        status=ToolStatus.SUCCESS, data="main.py\nREADME.md", message="ok"
    )

    with patch.object(
        provider,
        "_execute_tool",
        AsyncMock(
            return_value=(mock_result, {"status": "success", "result": "main.py"})
        ),
    ):
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
    tool1 = _tool_use_block("tu_1", "bash", {"command": "ls"})
    tool2 = _tool_use_block("tu_2", "bash", {"command": "cat README.md"})
    resp1 = _response([tool1], stop_reason="tool_use")
    resp2 = _response([tool2], stop_reason="tool_use")
    resp3 = _response([_text_block("done")], stop_reason="end_turn")

    provider._client.messages.create = AsyncMock(side_effect=[resp1, resp2, resp3])

    from deile.tools.base import ToolResult, ToolStatus

    mock_tr = ToolResult(status=ToolStatus.SUCCESS, data="ok")

    with patch.object(
        provider,
        "_execute_tool",
        AsyncMock(return_value=(mock_tr, {"status": "success", "result": "ok"})),
    ):
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
    import anthropic as _ant

    err = _ant.AuthenticationError(
        message="Invalid API key",
        response=MagicMock(status_code=401, headers={}),
        body={"error": {"type": "authentication_error", "message": "Invalid API key"}},
    )
    provider._client.messages.create = AsyncMock(side_effect=err)

    with pytest.raises(ProviderInvocationError) as exc_info:
        await provider.generate([ModelMessage(role="user", content="hi")])

    envelope = exc_info.value.envelope
    assert envelope.provider_id == "anthropic"
    assert envelope.error_type == "auth"
    assert envelope.http_status == 401
    assert isinstance(envelope.raw_json, dict)


# ---------------------------------------------------------------------------
# Test: streaming — yields TEXT_DELTA + USAGE_FINAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_stream_events(provider):
    """generate_stream yields TEXT_DELTA events and ends with USAGE_FINAL."""

    # Build mock stream context manager
    delta1 = MagicMock()
    delta1.type = "text_delta"
    delta1.text = "Hello"

    delta2 = MagicMock()
    delta2.type = "text_delta"
    delta2.text = " World"

    ev1 = MagicMock()
    ev1.type = "content_block_delta"
    ev1.delta = delta1

    ev2 = MagicMock()
    ev2.type = "content_block_delta"
    ev2.delta = delta2

    ev_stop = MagicMock()
    ev_stop.type = "message_stop"

    final_msg = MagicMock()
    final_msg.usage = _usage(inp=5, out=10)

    async def _aiter(self_):
        for e in [ev1, ev2, ev_stop]:
            yield e

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_cm)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    stream_cm.__aiter__ = _aiter
    stream_cm.get_final_message = AsyncMock(return_value=final_msg)

    provider._client.messages.stream = MagicMock(return_value=stream_cm)

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
    usage = ModelUsage(
        prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000
    )
    cost = provider.estimate_cost(usage)
    # 1M input @ $5.00 + 1M output @ $25.00 = $30.00
    assert abs(cost - 30.00) < 1e-4


# ---------------------------------------------------------------------------
# Test: provider_id and tier
# ---------------------------------------------------------------------------


def test_provider_id(provider):
    assert provider.provider_id == "anthropic"


def test_tier(provider, handle):
    assert provider.tier == ModelTier.TIER_1


def test_pricing(provider, handle):
    assert provider.pricing.input_per_1m_usd == 5.00
    assert provider.pricing.output_per_1m_usd == 25.00
