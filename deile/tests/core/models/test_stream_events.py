"""Validate the streaming event contract that the agent + UI rely on."""

from __future__ import annotations

from deile.core.models.stream_events import (
    ModelUsageSnapshot,
    StreamEventType,
    UnifiedStreamEvent,
)


class TestStreamEventType:
    def test_includes_tool_result(self):
        assert StreamEventType.TOOL_RESULT.value == "tool_result"

    def test_canonical_set(self):
        names = {e.name for e in StreamEventType}
        assert names == {
            "TEXT_DELTA",
            "TOOL_USE_START",
            "TOOL_USE_DELTA",
            "TOOL_USE_END",
            "TOOL_RESULT",
            "USAGE_FINAL",
            "ERROR",
            "STAGE",
            "PROGRESS",
            "RICH_RENDERABLE",
        }


class TestUnifiedStreamEvent:
    def test_text_delta_minimal(self):
        e = UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hi")
        assert e.text == "hi"
        assert e.tool_call_id is None
        assert e.iteration is None

    def test_tool_result_fields(self):
        e = UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="bash",
            tool_status="success",
            tool_result_summary="exit 0",
            tool_result_data={"stdout": "ok"},
            iteration=2,
        )
        assert e.tool_status == "success"
        assert e.tool_result_summary == "exit 0"
        assert e.tool_result_data == {"stdout": "ok"}
        assert e.iteration == 2

    def test_usage_final(self):
        e = UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=10, output_tokens=20, cost_usd=0.001),
        )
        assert e.usage.input_tokens == 10
        assert e.usage.output_tokens == 20
        assert e.usage.cost_usd == 0.001

    def test_validation_gate_marker(self):
        e = UnifiedStreamEvent(
            type=StreamEventType.TEXT_DELTA,
            text="P.S.",
            source="validation_gate",
        )
        assert e.source == "validation_gate"
