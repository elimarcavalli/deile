"""Tests for process_input_stream_chunks chunk integrity."""

from __future__ import annotations

import pytest

from deile.core.agent import DeileAgent
from deile.core.bot_streaming import StreamChunk


class FakeStreamEvent:
    def __init__(self, type_name, **attrs):
        from deile.core.models.stream_events import StreamEventType

        self.type = StreamEventType[type_name]
        for k, v in attrs.items():
            setattr(self, k, v)


async def fake_stream_yields(*events):
    async def gen(self, *args, **kwargs):
        for e in events:
            yield e
    return gen


class TestStreamChunks:
    async def test_text_recombines_to_done(self, monkeypatch):
        agent = DeileAgent()

        async def fake_stream(self, user_input, session_id="default", **kwargs):
            yield FakeStreamEvent("TEXT_DELTA", text="Hello ")
            yield FakeStreamEvent("TEXT_DELTA", text="world")
            yield FakeStreamEvent("USAGE_FINAL")

        monkeypatch.setattr(DeileAgent, "process_input_stream", fake_stream)
        chunks = []
        async for c in agent.process_input_stream_chunks("oi"):
            chunks.append(c)
        # Should end with `done`
        assert chunks[-1].kind == "done"
        # Recombined text matches done payload
        text_chunks = [c for c in chunks if c.kind == "text"]
        recombined = "".join(c.payload["text"] for c in text_chunks)
        assert recombined == chunks[-1].payload["text"] == "Hello world"

    async def test_done_always_last(self, monkeypatch):
        agent = DeileAgent()

        async def fake_stream(self, user_input, session_id="default", **kwargs):
            yield FakeStreamEvent("TEXT_DELTA", text="x")

        monkeypatch.setattr(DeileAgent, "process_input_stream", fake_stream)
        chunks = []
        async for c in agent.process_input_stream_chunks("y"):
            chunks.append(c)
        assert chunks[-1].kind == "done"

    async def test_error_then_done_on_exception(self, monkeypatch):
        agent = DeileAgent()

        async def fake_stream(self, user_input, session_id="default", **kwargs):
            yield FakeStreamEvent("TEXT_DELTA", text="hello")
            raise RuntimeError("boom")

        monkeypatch.setattr(DeileAgent, "process_input_stream", fake_stream)
        chunks = []
        async for c in agent.process_input_stream_chunks("oi"):
            chunks.append(c)
        kinds = [c.kind for c in chunks]
        assert "error" in kinds
        assert kinds[-1] == "done"
