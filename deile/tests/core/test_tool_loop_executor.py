"""ToolLoopExecutor tests — the protected piece of the streaming refactor.

A FakeProvider emits scripted streams; the executor must:
  * forward every event,
  * collect TOOL_USE_END calls,
  * execute via a stubbed registry,
  * emit TOOL_RESULT events,
  * round-trip the result message back to the provider on the next iteration,
  * stop on MAX_ITERATIONS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from deile.core.models.base import ModelMessage
from deile.core.models.stream_events import (ModelUsageSnapshot,
                                             StreamEventType,
                                             UnifiedStreamEvent)
from deile.core.tool_loop_executor import ToolLoopExecutor
from deile.tools.base import ToolResult, ToolStatus

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeProvider:
    """Provider stub: replays a queue of event-lists per iteration."""

    provider_id: str = "fake"
    iterations: List[List[UnifiedStreamEvent]] = field(default_factory=list)
    seen_messages: List[List[ModelMessage]] = field(default_factory=list)
    _idx: int = 0

    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        self.seen_messages.append(list(messages))
        if self._idx >= len(self.iterations):
            return
        events = self.iterations[self._idx]
        self._idx += 1
        for event in events:
            yield event

    def format_assistant_tool_use_message(
        self,
        pending_tool_calls,
        text_so_far: str = "",
        reasoning_content: Optional[str] = None,
    ) -> ModelMessage:
        meta: Dict[str, Any] = {"_tool_calls": list(pending_tool_calls)}
        if reasoning_content:
            meta["reasoning_content"] = reasoning_content
        return ModelMessage(role="assistant", content=text_so_far, metadata=meta)

    def format_tool_result_message(
        self, tool_call_id: str, tool_name: str, payload: Any
    ) -> ModelMessage:
        return ModelMessage(
            role="tool",
            content=str(payload),
            metadata={"tool_call_id": tool_call_id, "tool_name": tool_name},
        )


@dataclass
class FakeRegistry:
    """Drop-in for ToolRegistry.execute_tool — returns scripted ToolResults."""

    results: Dict[str, ToolResult] = field(default_factory=dict)
    seen: List[str] = field(default_factory=list)

    async def execute_tool(self, name: str, ctx) -> ToolResult:
        self.seen.append(name)
        if name in self.results:
            return self.results[name]
        return ToolResult(status=ToolStatus.SUCCESS, data={"echo": name}, message="ok")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_calls_returns_after_first_iteration():
    provider = FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hi"),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=ModelUsageSnapshot(),
                ),
            ]
        ]
    )
    executor = ToolLoopExecutor(tool_registry=FakeRegistry())
    events = []
    async for evt in executor.run(provider, [ModelMessage(role="user", content="hi")], tools=[]):
        events.append(evt)
    # The executor opens each iteration with a STAGE event so the UI can
    # show "Awaiting first token from <provider>" instead of going blank.
    types = [e.type for e in events]
    assert types[0] is StreamEventType.STAGE
    assert types[1:] == [StreamEventType.TEXT_DELTA, StreamEventType.USAGE_FINAL]


@pytest.mark.asyncio
async def test_tool_call_emits_result_and_round_trips():
    registry = FakeRegistry(
        results={
            "list_files": ToolResult(
                status=ToolStatus.SUCCESS,
                data="a.py\nb.py",
                message="2 files",
            )
        }
    )
    provider = FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Let me list. "),
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_START,
                    tool_call_id="t1",
                    tool_name="list_files",
                ),
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id="t1",
                    tool_name="list_files",
                    arguments={"path": "."},
                ),
            ],
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Done."),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL, usage=ModelUsageSnapshot()
                ),
            ],
        ]
    )
    executor = ToolLoopExecutor(tool_registry=registry)
    events = []
    async for evt in executor.run(provider, [ModelMessage(role="user", content="ls")], tools=[]):
        events.append(evt)

    types = [e.type for e in events]
    # Expected sequence: text, tool_use_start, tool_use_end, tool_result, text, usage
    assert StreamEventType.TOOL_USE_START in types
    assert StreamEventType.TOOL_USE_END in types
    assert StreamEventType.TOOL_RESULT in types
    tr_event = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    assert tr_event.tool_status == "success"
    assert tr_event.tool_call_id == "t1"
    # Summary collapses the data payload to a single line
    assert "a.py" in (tr_event.tool_result_summary or "")
    # Raw payload preserved for rich-display consumers
    assert tr_event.tool_result_data == "a.py\nb.py"
    # Registry was actually called
    assert registry.seen == ["list_files"]
    # Iteration 2 saw the tool_result message in history
    assert len(provider.seen_messages) == 2
    msgs_iter2 = provider.seen_messages[1]
    assert any(m.role == "tool" for m in msgs_iter2)


@pytest.mark.asyncio
async def test_tool_failure_emits_error_status():
    registry = FakeRegistry(
        results={
            "broken": ToolResult(
                status=ToolStatus.ERROR,
                message="kaboom",
            )
        }
    )
    provider = FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_START,
                    tool_call_id="t1",
                    tool_name="broken",
                ),
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id="t1",
                    tool_name="broken",
                    arguments={},
                ),
            ],
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Sorry."),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL, usage=ModelUsageSnapshot()
                ),
            ],
        ]
    )
    executor = ToolLoopExecutor(tool_registry=registry)
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    tr = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    assert tr.tool_status == "error"
    assert "kaboom" in (tr.tool_result_summary or "")


@pytest.mark.asyncio
async def test_error_event_aborts_loop():
    provider = FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="part "),
                UnifiedStreamEvent(
                    type=StreamEventType.ERROR,
                    error_envelope={"message": "auth"},
                ),
            ],
            # Iteration 2 should never be requested
            [UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="never")],
        ]
    )
    executor = ToolLoopExecutor(tool_registry=FakeRegistry())
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    assert events[-1].type == StreamEventType.ERROR
    # Provider was only invoked once
    assert len(provider.seen_messages) == 1


@pytest.mark.asyncio
async def test_max_iterations_caps_loop(monkeypatch):
    # Build many iterations of "always asks for a tool"
    one_round = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="tx",
            tool_name="echo",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="tx",
            tool_name="echo",
            arguments={},
        ),
    ]
    provider = FakeProvider(iterations=[list(one_round) for _ in range(20)])
    executor = ToolLoopExecutor(tool_registry=FakeRegistry(), max_iterations=3)
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    # Provider was called exactly max_iterations times — never the 4th
    assert len(provider.seen_messages) == 3
    # And exactly 3 TOOL_RESULT events were emitted
    assert sum(1 for e in events if e.type == StreamEventType.TOOL_RESULT) == 3


def test_max_iterations_defaults_to_configured_setting(monkeypatch):
    # No explicit cap → resolved from settings (DEILE_MAX_TOOL_ITERATIONS /
    # agent.max_tool_iterations). Raised from 25 to 100 so a real implementation
    # turn (read files + edit + test + commit + push + open PR) isn't truncated.
    from deile.config.settings import get_settings
    from deile.core.tool_loop_executor import _resolve_max_iterations

    settings = get_settings()
    monkeypatch.setattr(settings, "max_tool_iterations", 137, raising=False)
    assert _resolve_max_iterations() == 137
    assert ToolLoopExecutor(tool_registry=FakeRegistry())._max_iterations == 137
    # An explicit value always wins over the configured setting.
    assert (
        ToolLoopExecutor(tool_registry=FakeRegistry(), max_iterations=5)._max_iterations
        == 5
    )


@pytest.mark.asyncio
async def test_iteration_field_is_set_on_events():
    provider = FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="a"),
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id="t1",
                    tool_name="echo",
                    arguments={},
                ),
            ],
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="b"),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=ModelUsageSnapshot(),
                ),
            ],
        ]
    )
    executor = ToolLoopExecutor(tool_registry=FakeRegistry())
    events = [
        e
        async for e in executor.run(
            provider, [ModelMessage(role="user", content="x")], tools=[]
        )
    ]
    iter0_events = [e for e in events if e.iteration == 0]
    iter1_events = [e for e in events if e.iteration == 1]
    assert iter0_events
    assert iter1_events
    # TOOL_RESULT carries iteration=0 (it ran after iter0)
    tr = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    assert tr.iteration == 0


@pytest.mark.asyncio
async def test_event_publisher_called_for_each_tool():
    calls: List[tuple] = []

    async def publisher(kind: str, name: str, **kw):
        calls.append((kind, name))

    provider = FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id="t1",
                    tool_name="echo",
                    arguments={},
                ),
            ],
            [
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL, usage=ModelUsageSnapshot()
                )
            ],
        ]
    )
    executor = ToolLoopExecutor(tool_registry=FakeRegistry(), event_publisher=publisher)
    async for _ in executor.run(provider, [ModelMessage(role="user", content="x")], tools=[]):
        pass
    kinds = [c[0] for c in calls]
    assert "invoked" in kinds
    assert "completed" in kinds
