"""Gemini provider streaming tool-aware tests.

Gemini SDK doesn't support char-level streaming with function-calling enabled
in the version we use, so the streaming UX is "lumpy": the tool path emits a
single TEXT_DELTA + TOOL_USE_START/END pair per round-trip.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def gemini_provider(monkeypatch):
    """Construct a GeminiProvider without hitting the network."""
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.gemini_provider import GeminiProvider
    from deile.core.models.provider_config import ProviderConfig
    from deile.core.models.tier import ModelTier

    handle = ModelHandle(
        provider_id="gemini",
        model_id="gemini-2.5-pro",
        tier=ModelTier.TIER_1,
        pricing=ModelPricing(input_per_1m_usd=1.25, output_per_1m_usd=5.0),
        context_window=2_000_000,
        capabilities=frozenset({"function_calling", "vision"}),
        display_name="Gemini 2.5 Pro",
        label="flagship",
    )
    cfg = ProviderConfig(
        provider_id="gemini", api_key_env="GOOGLE_API_KEY", base_url=None
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    with patch("deile.core.models.gemini_provider.genai"):
        provider = GeminiProvider(handle, cfg)
    return provider


def test_format_tool_result_message_uses_function_response_metadata(gemini_provider):
    msg = gemini_provider.format_tool_result_message("t1", "list_files", {"x": 1})
    fr = msg.metadata["_gemini_function_response"]
    assert fr["name"] == "list_files"
    assert fr["response"] == {"x": 1}
    assert fr["tool_call_id"] == "t1"


def test_format_assistant_tool_use_message_marks_history_owned_by_sdk(gemini_provider):
    msg = gemini_provider.format_assistant_tool_use_message(
        [("t1", "echo", {})], text_so_far=""
    )
    assert msg.metadata["_gemini_history_owned_by_sdk"] is True


@pytest.mark.asyncio
async def test_streaming_with_tools_emits_tool_use_events(gemini_provider):
    """When ``tools`` is provided, the lumpy stream must surface
    TOOL_USE_START/TOOL_USE_END for each function_call in the response."""
    from deile.core.models.base import ModelMessage
    from deile.core.models.stream_events import StreamEventType

    fake_response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text=None,
                            function_call=SimpleNamespace(
                                name="list_files",
                                args={"path": "."},
                            ),
                        )
                    ]
                )
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=2
        ),
    )

    fake_chat = MagicMock()
    fake_chat.send_message = MagicMock(return_value=fake_response)

    with patch.object(
        gemini_provider, "create_chat_session", new=AsyncMock(return_value=fake_chat)
    ):
        out = []
        async for evt in gemini_provider.generate_stream(
            [ModelMessage(role="user", content="ls")],
            tools=[MagicMock()],
        ):
            out.append(evt)

    types = [e.type for e in out]
    assert StreamEventType.TOOL_USE_START in types
    assert StreamEventType.TOOL_USE_END in types
    end = next(e for e in out if e.type is StreamEventType.TOOL_USE_END)
    assert end.tool_name == "list_files"
    assert end.arguments == {"path": "."}
    assert StreamEventType.USAGE_FINAL in types


@pytest.mark.asyncio
async def test_streaming_without_tools_emits_single_text_delta(gemini_provider):
    """When ``tools=None``, the provider emits the completed text as a single
    TEXT_DELTA (no artificial word-chunking) — we assert that text events are
    produced and a USAGE_FINAL closes the stream."""
    from deile.core.models.base import ModelMessage
    from deile.core.models.stream_events import StreamEventType

    fake_response = SimpleNamespace(
        content="alpha beta gamma delta",
        usage=SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=4,
            cached_tokens=0,
            cost_estimate=0.0,
        ),
    )
    with patch.object(
        gemini_provider, "generate", new=AsyncMock(return_value=fake_response)
    ):
        out = []
        async for evt in gemini_provider.generate_stream(
            [ModelMessage(role="user", content="x")],
            tools=None,
        ):
            out.append(evt)
    types = [e.type for e in out]
    assert StreamEventType.TEXT_DELTA in types
    assert types[-1] == StreamEventType.USAGE_FINAL
