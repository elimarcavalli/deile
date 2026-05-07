"""Tests for process_input_structured."""

from __future__ import annotations

from deile.common.markup_ast import SpanKind
from deile.core.agent import AgentResponse, AgentStatus, DeileAgent
from deile.core.bot_streaming import StructuredResponse


class TestStructuredResponse:
    def test_dto_roundtrip(self):
        from deile.common.markup_ast import MarkupAST

        sr = StructuredResponse(
            text="hello",
            markup=MarkupAST.from_plain("hello"),
            tool_calls=[],
            elapsed_ms=10,
            model_used="fake",
        )
        assert sr.text == "hello"
        assert len(sr.markup) == 1


class TestStructuredCallsProcessInput:
    async def test_returns_parsed_ast(self, tmp_path, monkeypatch):
        agent = DeileAgent()

        async def fake_process_input(self, user_input, session_id="default", **kwargs):
            return AgentResponse(
                content="**bold** text\n- item1\n- item2",
                status=AgentStatus.IDLE,
                tool_results=[],
                metadata={"model_used": "fake-model"},
                execution_time=0.05,
            )

        monkeypatch.setattr(DeileAgent, "process_input", fake_process_input)
        sr = await agent.process_input_structured("oi", session_id="t1")
        assert isinstance(sr, StructuredResponse)
        assert sr.text == "**bold** text\n- item1\n- item2"
        kinds = {span.kind for span in sr.markup}
        assert SpanKind.BOLD in kinds
        assert SpanKind.BULLET in kinds
        assert sr.elapsed_ms >= 0
        assert sr.model_used == "fake-model"
