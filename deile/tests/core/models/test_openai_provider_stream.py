"""OpenAI/DeepSeek streaming tool-aware tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

import pytest

from deile.core.models.base import ModelMessage
from deile.core.models.stream_events import StreamEventType


def _make_provider(monkeypatch, provider_cls_name: str = "OpenAIProvider"):
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.deepseek_provider import DeepSeekProvider
    from deile.core.models.openai_provider import OpenAIProvider
    from deile.core.models.provider_config import ProviderConfig
    from deile.core.models.tier import ModelTier

    handle = ModelHandle(
        provider_id="openai",
        model_id="gpt-4o",
        tier=ModelTier.TIER_1,
        pricing=ModelPricing(input_per_1m_usd=2.5, output_per_1m_usd=10.0),
        context_window=128_000,
        capabilities=frozenset({"streaming", "function_calling"}),
        display_name="GPT-4o",
        label="flagship",
    )
    cfg = ProviderConfig(
        provider_id="openai", api_key_env="OPENAI_API_KEY", base_url=None
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    cls = OpenAIProvider if provider_cls_name == "OpenAIProvider" else DeepSeekProvider
    if cls is DeepSeekProvider:
        cfg = ProviderConfig(
            provider_id="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com/v1",
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    return cls(model_handle=handle, provider_config=cfg)


@pytest.fixture
def provider(monkeypatch):
    return _make_provider(monkeypatch)


async def _replay(chunks: List):
    for c in chunks:
        yield c


def _chunk_text(text: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _chunk_finish_with_usage(prompt_tokens=10, completion_tokens=2):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(delta=None, finish_reason="stop"),
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


@pytest.mark.asyncio
async def test_text_stream_emits_text_and_usage(provider):
    chunks = [
        _chunk_text("hello "),
        _chunk_text("world"),
        _chunk_finish_with_usage(),
    ]

    async def fake_create(**kwargs):
        return _replay(chunks)

    with patch.object(
        provider._client.chat.completions, "create", side_effect=fake_create
    ):
        out = []
        async for evt in provider.generate_stream(
            [ModelMessage(role="user", content="x")], tools=None
        ):
            out.append(evt)
    types = [o.type for o in out]
    assert types == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.USAGE_FINAL,
    ]


@pytest.mark.asyncio
async def test_tool_call_stream_emits_lifecycle(provider):
    def _delta_tool_call(index, id_=None, name=None, arguments=None):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=index,
                                id=id_,
                                function=SimpleNamespace(
                                    name=name, arguments=arguments
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )

    def _finish(reason: str):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=None, finish_reason=reason)],
            usage=None,
        )

    chunks = [
        _delta_tool_call(0, id_="call-1", name="list_files"),
        _delta_tool_call(0, arguments='{"path":'),
        _delta_tool_call(0, arguments='"."}'),
        _finish("tool_calls"),
        _chunk_finish_with_usage(),
    ]

    async def fake_create(**kwargs):
        return _replay(chunks)

    with patch.object(
        provider._client.chat.completions, "create", side_effect=fake_create
    ):
        out = []
        async for evt in provider.generate_stream(
            [ModelMessage(role="user", content="ls")], tools=None
        ):
            out.append(evt)

    types = [o.type for o in out]
    assert StreamEventType.TOOL_USE_START in types
    assert StreamEventType.TOOL_USE_END in types
    end_evt = next(o for o in out if o.type is StreamEventType.TOOL_USE_END)
    assert end_evt.tool_call_id == "call-1"
    assert end_evt.tool_name == "list_files"
    assert end_evt.arguments == {"path": "."}
    # START must fire immediately before END (back-to-back at finish_reason time)
    # so the renderer always shows args the instant the tool block appears.
    start_idx = next(
        i for i, e in enumerate(out) if e.type is StreamEventType.TOOL_USE_START
    )
    end_idx = next(
        i for i, e in enumerate(out) if e.type is StreamEventType.TOOL_USE_END
    )
    assert (
        end_idx == start_idx + 1
    ), "TOOL_USE_END must immediately follow TOOL_USE_START"


def test_format_assistant_tool_use_message_structure(provider):
    msg = provider.format_assistant_tool_use_message(
        [("c1", "echo", {"x": 1})], text_so_far=""
    )
    tcs = msg.metadata["_openai_tool_calls"]
    assert tcs[0]["id"] == "c1"
    assert tcs[0]["function"]["name"] == "echo"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"x": 1}


def test_format_tool_result_message_round_trip(provider):
    msg = provider.format_tool_result_message("c1", "echo", {"ok": True})
    tr = msg.metadata["_openai_tool_result"]
    assert tr["tool_call_id"] == "c1"
    assert "ok" in tr["content"]


def test_deepseek_subclass_inherits_streaming():
    """DeepSeek inherits generate_stream from OpenAIProvider — guard against
    accidental override that breaks the unified contract."""
    from deile.core.models.deepseek_provider import DeepSeekProvider
    from deile.core.models.openai_provider import OpenAIProvider

    assert DeepSeekProvider.generate_stream is OpenAIProvider.generate_stream
    assert (
        DeepSeekProvider.format_tool_result_message
        is OpenAIProvider.format_tool_result_message
    )
    assert (
        DeepSeekProvider.format_assistant_tool_use_message
        is OpenAIProvider.format_assistant_tool_use_message
    )


def _chunk_with_reasoning(reasoning: str, content: str = ""):
    """Simulate a DeepSeek-R1 streaming chunk with reasoning_content."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content or None,
                    reasoning_content=reasoning,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


@pytest.mark.asyncio
async def test_reasoning_content_emitted_in_usage_final_for_stop_path(provider):
    """When a provider streams delta.reasoning_content and finishes with 'stop',
    the accumulated text must appear in the USAGE_FINAL event's reasoning_content
    field so agent.py can store it for the next turn."""
    chunks = [
        _chunk_with_reasoning("<think>step1</think>"),
        _chunk_with_reasoning("<think>step2</think>", content="Hi"),
        _chunk_finish_with_usage(),
    ]

    async def fake_create(**kwargs):
        return _replay(chunks)

    with patch.object(
        provider._client.chat.completions, "create", side_effect=fake_create
    ):
        out = []
        async for evt in provider.generate_stream(
            [ModelMessage(role="user", content="x")], tools=None
        ):
            out.append(evt)

    usage_evt = next(o for o in out if o.type is StreamEventType.USAGE_FINAL)
    assert usage_evt.reasoning_content == "<think>step1</think><think>step2</think>"


@pytest.mark.asyncio
async def test_reasoning_content_emitted_in_tool_use_end(provider):
    """When finish_reason=='tool_calls', reasoning_content must be attached to
    every TOOL_USE_END event so ToolLoopExecutor can pass it to
    format_assistant_tool_use_message for the next iteration."""

    def _tc_delta(index, id_=None, name=None, arguments=None, reasoning=None):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning_content=reasoning,
                        tool_calls=[
                            SimpleNamespace(
                                index=index,
                                id=id_,
                                function=SimpleNamespace(
                                    name=name, arguments=arguments
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )

    def _finish_tool_calls():
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=None, finish_reason="tool_calls")],
            usage=None,
        )

    chunks = [
        _chunk_with_reasoning("<think>I need a tool</think>"),
        _tc_delta(0, id_="call-1", name="echo", arguments='{"msg":"hi"}'),
        _finish_tool_calls(),
        _chunk_finish_with_usage(),
    ]

    async def fake_create(**kwargs):
        return _replay(chunks)

    with patch.object(
        provider._client.chat.completions, "create", side_effect=fake_create
    ):
        out = []
        async for evt in provider.generate_stream(
            [ModelMessage(role="user", content="x")], tools=None
        ):
            out.append(evt)

    end_evt = next(o for o in out if o.type is StreamEventType.TOOL_USE_END)
    assert end_evt.reasoning_content == "<think>I need a tool</think>"


def test_format_assistant_tool_use_message_preserves_reasoning_content(provider):
    """reasoning_content kwarg must be stored in metadata so _to_openai_messages
    can echo it back to the API on the next round-trip."""
    msg = provider.format_assistant_tool_use_message(
        [("c1", "echo", {"x": 1})],
        text_so_far="",
        reasoning_content="<think>some reasoning</think>",
    )
    assert msg.metadata["reasoning_content"] == "<think>some reasoning</think>"
    # Verify _to_openai_messages includes it in the outgoing dict.
    oai = provider._to_openai_messages([msg], system_instruction=None)
    assistant_msg = next(m for m in oai if m["role"] == "assistant")
    assert assistant_msg.get("reasoning_content") == "<think>some reasoning</think>"
