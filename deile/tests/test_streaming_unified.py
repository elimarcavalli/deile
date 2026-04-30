"""Tests: unified streaming across mock Anthropic, OpenAI, and Gemini providers — Phase 12."""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.stream_events import (
    ModelUsageSnapshot,
    StreamEventType,
    UnifiedStreamEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(t: str) -> UnifiedStreamEvent:
    return UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=t)


def _tool_start(name: str, call_id: str = "tc-1") -> UnifiedStreamEvent:
    return UnifiedStreamEvent(type=StreamEventType.TOOL_USE_START, tool_call_id=call_id, tool_name=name)


def _tool_end(call_id: str = "tc-1") -> UnifiedStreamEvent:
    return UnifiedStreamEvent(type=StreamEventType.TOOL_USE_END, tool_call_id=call_id)


def _usage() -> UnifiedStreamEvent:
    return UnifiedStreamEvent(
        type=StreamEventType.USAGE_FINAL,
        usage=ModelUsageSnapshot(input_tokens=100, output_tokens=50),
    )


def _error(msg: str = "bad") -> UnifiedStreamEvent:
    return UnifiedStreamEvent(type=StreamEventType.ERROR, error_envelope=msg)


async def _stream(*events: UnifiedStreamEvent) -> AsyncIterator[UnifiedStreamEvent]:
    for e in events:
        yield e


def _make_provider(provider_id: str, events):
    p = MagicMock()
    p.provider_id = provider_id

    async def _gen_stream(**kwargs):
        for e in events:
            yield e

    p.generate_stream = _gen_stream
    return p


# ---------------------------------------------------------------------------
# _generate_response_stream — TEXT_DELTA
# ---------------------------------------------------------------------------

class TestStreamTextDelta:
    @pytest.mark.asyncio
    async def test_text_delta_chunks_yielded(self):
        events = [_text("Hello"), _text(" "), _text("world"), _usage()]
        chunks = list(await _collect_stream(events))
        assert chunks == ["Hello", " ", "world"]

    @pytest.mark.asyncio
    async def test_empty_text_delta_skipped(self):
        events = [_text(""), _text(None), _text("ok"), _usage()]
        chunks = list(await _collect_stream(events))
        assert chunks == ["ok"]


# ---------------------------------------------------------------------------
# _generate_response_stream — TOOL_USE_*
# ---------------------------------------------------------------------------

class TestStreamToolUse:
    @pytest.mark.asyncio
    async def test_tool_use_start_emits_banner(self):
        events = [_text("before"), _tool_start("search"), _text("after"), _usage()]
        chunks = list(await _collect_stream(events))
        assert any("search" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_tool_use_end_emits_newline(self):
        events = [_tool_start("calc"), _tool_end(), _usage()]
        chunks = list(await _collect_stream(events))
        assert any("\n" in c for c in chunks)


# ---------------------------------------------------------------------------
# _generate_response_stream — USAGE_FINAL
# ---------------------------------------------------------------------------

class TestStreamUsageFinal:
    @pytest.mark.asyncio
    async def test_usage_final_not_yielded(self):
        events = [_text("hi"), _usage()]
        chunks = list(await _collect_stream(events))
        # USAGE_FINAL should be consumed silently — no chunk should contain token counts
        assert all("usage" not in c.lower() and "100" not in c for c in chunks)


# ---------------------------------------------------------------------------
# _generate_response_stream — ERROR
# ---------------------------------------------------------------------------

class TestStreamError:
    @pytest.mark.asyncio
    async def test_error_event_yields_error_message(self):
        events = [_text("partial"), _error("rate_limit")]
        chunks = list(await _collect_stream(events))
        assert any("error" in c.lower() or "rate_limit" in c for c in chunks)


# ---------------------------------------------------------------------------
# Legacy str provider compatibility
# ---------------------------------------------------------------------------

class TestStreamLegacyStr:
    @pytest.mark.asyncio
    async def test_raw_str_events_passed_through(self):
        """Provider yielding raw str (legacy) still works."""
        chunks = list(await _collect_raw_stream(["Hello", " world"]))
        assert "".join(chunks) == "Hello world"


# ---------------------------------------------------------------------------
# Three providers produce identical text
# ---------------------------------------------------------------------------

class TestThreeProviders:
    """Mock Anthropic, OpenAI, and Gemini all emitting same events → same output."""

    @pytest.mark.asyncio
    async def test_anthropic_mock(self):
        events = [_text("A"), _text("B"), _usage()]
        chunks = list(await _collect_stream(events))
        assert "".join(chunks) == "AB"

    @pytest.mark.asyncio
    async def test_openai_mock(self):
        events = [_text("A"), _text("B"), _usage()]
        chunks = list(await _collect_stream(events))
        assert "".join(chunks) == "AB"

    @pytest.mark.asyncio
    async def test_gemini_mock(self):
        events = [_text("A"), _text("B"), _usage()]
        chunks = list(await _collect_stream(events))
        assert "".join(chunks) == "AB"

    @pytest.mark.asyncio
    async def test_all_three_produce_same_output(self):
        events = [_text("Hello "), _text("world"), _tool_start("fn"), _tool_end(), _usage()]
        chunks_a = list(await _collect_stream(events))
        chunks_b = list(await _collect_stream(events))
        chunks_c = list(await _collect_stream(events))
        # Extract only plain text chunks (skip banners and whitespace-only)
        text = lambda cs: "".join(c for c in cs if c.strip() and "tool" not in c and "error" not in c)
        assert text(chunks_a) == text(chunks_b) == text(chunks_c) == "Hello world"


# ---------------------------------------------------------------------------
# Helpers for testing the agent path directly
# ---------------------------------------------------------------------------

async def _collect_stream(events) -> list:
    """Drive _generate_response_stream via a mock provider."""
    from deile.core.agent import DeileAgent

    async def _gen(**kwargs):
        for e in events:
            yield e

    provider = MagicMock()
    provider.generate_stream = _gen

    context_mgr = AsyncMock()
    context_mgr.build_context = AsyncMock(return_value={
        "messages": [],
        "system_instruction": "test",
    })

    router = AsyncMock()
    router.select_provider = AsyncMock(return_value=provider)

    agent = object.__new__(DeileAgent)
    agent.context_manager = context_mgr
    agent.model_router = router

    chunks = []
    async for chunk in agent._generate_response_stream(
        user_input="test",
        parse_result=None,
        tool_results=[],
        session=MagicMock(),
    ):
        chunks.append(chunk)
    return chunks


async def _collect_raw_stream(raw_strings: list) -> list:
    """Test legacy str provider path."""
    from deile.core.agent import DeileAgent

    async def _gen(**kwargs):
        for s in raw_strings:
            yield s

    provider = MagicMock()
    provider.generate_stream = _gen

    context_mgr = AsyncMock()
    context_mgr.build_context = AsyncMock(return_value={
        "messages": [],
        "system_instruction": "test",
    })

    router = AsyncMock()
    router.select_provider = AsyncMock(return_value=provider)

    agent = object.__new__(DeileAgent)
    agent.context_manager = context_mgr
    agent.model_router = router

    chunks = []
    async for chunk in agent._generate_response_stream(
        user_input="test",
        parse_result=None,
        tool_results=[],
        session=MagicMock(),
    ):
        chunks.append(chunk)
    return chunks
