"""AnthropicProvider streaming tool-aware tests.

Mocks the Anthropic SDK stream so the assertion is the SDK→UnifiedStreamEvent
mapping, not network behaviour.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

import pytest

from deile.core.models.base import ModelMessage
from deile.core.models.stream_events import StreamEventType


def _make_provider():
    from deile.core.models.anthropic_provider import AnthropicProvider
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.tier import ModelTier
    from deile.core.models.provider_config import ProviderConfig

    handle = ModelHandle(
        provider_id="anthropic",
        model_id="claude-opus-4-7",
        tier=ModelTier.TIER_1,
        pricing=ModelPricing(input_per_1m_usd=15.0, output_per_1m_usd=75.0),
        context_window=200_000,
        capabilities=frozenset({"streaming", "function_calling"}),
        display_name="Opus",
        label="flagship",
    )
    cfg = ProviderConfig(
        provider_id="anthropic", api_key_env="ANTHROPIC_API_KEY", base_url=None
    )
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-fake")
    return AnthropicProvider(model_handle=handle, provider_config=cfg)


def _e(etype, **kw):
    return SimpleNamespace(type=etype, **kw)


@asynccontextmanager
async def _fake_stream(events: List, final_message):
    class _S:
        def __init__(self):
            self._events = events
            self._final = final_message

        def __aiter__(self):
            async def gen():
                for e in self._events:
                    yield e

            return gen()

        async def get_final_message(self):
            return self._final

    yield _S()


@pytest.mark.asyncio
async def test_text_stream_only():
    provider = _make_provider()
    final = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
    )
    events = [
        _e("content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="hi")),
        _e("content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text=" world")),
        _e("message_stop"),
    ]

    def fake_stream(**kw):
        return _fake_stream(events, final)

    with patch.object(provider._client.messages, "stream", side_effect=fake_stream):
        out = []
        async for evt in provider.generate_stream(
            [ModelMessage(role="user", content="x")], tools=None
        ):
            out.append(evt)

    assert [o.type for o in out] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.USAGE_FINAL,
    ]
    assert out[0].text == "hi"
    assert out[1].text == " world"
    assert out[2].usage.input_tokens == 10


@pytest.mark.asyncio
async def test_tool_use_stream_emits_lifecycle():
    provider = _make_provider()
    final = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=8,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
    )
    events = [
        _e(
            "content_block_start",
            index=0,
            content_block=SimpleNamespace(type="tool_use", id="tool-1", name="bash_execute"),
        ),
        _e(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"command":'),
        ),
        _e(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json='"ls"}'),
        ),
        _e("content_block_stop", index=0),
        _e("message_stop"),
    ]

    def fake_stream(**kw):
        return _fake_stream(events, final)

    with patch.object(provider._client.messages, "stream", side_effect=fake_stream):
        out = []
        async for evt in provider.generate_stream(
            [ModelMessage(role="user", content="run ls")], tools=None
        ):
            out.append(evt)

    types = [o.type for o in out]
    assert StreamEventType.TOOL_USE_START in types
    assert StreamEventType.TOOL_USE_END in types
    end_evt = next(o for o in out if o.type is StreamEventType.TOOL_USE_END)
    assert end_evt.tool_call_id == "tool-1"
    assert end_evt.tool_name == "bash_execute"
    assert end_evt.arguments == {"command": "ls"}


def test_format_assistant_tool_use_message_structure():
    provider = _make_provider()
    msg = provider.format_assistant_tool_use_message(
        [("t1", "list_files", {"path": "."})], text_so_far="ok"
    )
    blocks = msg.metadata["_anthropic_content_blocks"]
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "ok"
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "t1"
    assert blocks[1]["input"] == {"path": "."}


def test_format_tool_result_message_round_trip():
    provider = _make_provider()
    msg = provider.format_tool_result_message("t1", "list_files", {"items": ["a.py"]})
    block = msg.metadata["_anthropic_content_blocks"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "t1"
    inner_text = block["content"][0]["text"]
    assert "a.py" in inner_text
