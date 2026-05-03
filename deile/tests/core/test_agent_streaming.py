"""Tests for ``DeileAgent.process_input_stream``.

These tests stub the model provider and tool registry, run a full streaming
turn end-to-end, and validate the contract — without spending tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.core.models.base import ModelMessage
from deile.core.models.stream_events import (
    ModelUsageSnapshot,
    StreamEventType,
    UnifiedStreamEvent,
)


@dataclass
class _FakeProvider:
    iterations: List[List[UnifiedStreamEvent]] = field(default_factory=list)
    provider_id: str = "fake"
    model_name: str = "fake-1"
    _idx: int = 0

    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        if self._idx < len(self.iterations):
            events = self.iterations[self._idx]
            self._idx += 1
            for evt in events:
                yield evt

    def format_assistant_tool_use_message(self, pending, text_so_far="", **kwargs):
        return ModelMessage(role="assistant", content=text_so_far)

    def format_tool_result_message(self, tool_call_id, tool_name, payload, **kwargs):
        return ModelMessage(role="tool", content=str(payload))


@pytest.fixture
def configured_agent(tmp_path: Path):
    """Build a DeileAgent with deterministic stubs.

    No autonomous processing, no workflow, no proactive tools — direct path
    to the streaming chat-with-tools loop.
    """
    from deile.core.agent import DeileAgent

    agent = DeileAgent.__new__(DeileAgent)
    # Minimal init
    agent.config_manager = None
    agent.tool_registry = MagicMock()
    agent.tool_registry.list_enabled = MagicMock(return_value=[])
    agent.tool_registry.execute_tool = AsyncMock()
    agent.parser_registry = MagicMock()
    agent.parser_registry.parse = AsyncMock(return_value=None)
    agent.context_manager = MagicMock()
    agent.context_manager.build_context = AsyncMock(
        return_value={
            "messages": [{"role": "user", "content": "hi"}],
            "system_instruction": None,
        }
    )
    agent.model_router = MagicMock()
    agent.model_router.providers = {}
    agent.model_router.select_provider = AsyncMock()
    agent.display_manager = MagicMock()
    agent.command_registry = MagicMock()
    agent.command_actions = MagicMock()

    from deile.config.settings import get_settings
    agent.settings = get_settings()
    agent.logger = MagicMock()

    from deile.core.agent import AgentStatus
    agent._status = AgentStatus.IDLE
    agent._sessions = {}
    agent._request_count = 0
    agent.persona_manager = None
    agent.persona_enhanced = False
    agent.workflow_executor = None
    agent.intent_analyzer = MagicMock()
    intent_result = MagicMock()
    intent_result.requires_workflow = MagicMock(return_value=False)
    agent.intent_analyzer.analyze = AsyncMock(return_value=intent_result)
    agent.proactive_analyzer = None

    # Stub out the autonomous and proactive paths so the streaming branch runs
    agent.process_autonomous_request = AsyncMock(return_value=None)
    agent._execute_proactive_tools = AsyncMock(return_value=[])
    agent._should_create_workflow = AsyncMock(return_value=False)

    # No-op validation gate (return content unchanged)
    async def _noop_gate(*, user_input, parse_result, session, content, tool_results):
        return content, tool_results
    agent._apply_validation_gate = _noop_gate

    # working_directory must exist
    agent._budget_guard_singleton = False  # disable budget checks

    return agent


@pytest.mark.asyncio
async def test_streaming_turn_yields_text_and_usage(configured_agent, tmp_path: Path):
    fake = _FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hello"),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=ModelUsageSnapshot(input_tokens=3, output_tokens=1),
                ),
            ]
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)

    session = configured_agent.create_session("s1", working_directory=str(tmp_path))
    events = []
    async for evt in configured_agent.process_input_stream(
        user_input="hi there", session_id=session.session_id
    ):
        events.append(evt)
    types = [e.type for e in events]
    assert StreamEventType.TEXT_DELTA in types
    assert StreamEventType.USAGE_FINAL in types
    # Session history captured the turn
    assert any(h["role"] == "assistant" for h in session.conversation_history)


@pytest.mark.asyncio
async def test_stage_events_emitted_before_first_content(configured_agent, tmp_path: Path):
    """The agent must emit STAGE events for each pre-stream phase BEFORE the
    first content event from the provider. This is what gives the user
    visibility into what the agent is doing during the "aguardando…" period
    instead of an opaque spinner.
    """
    fake = _FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hi"),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
                ),
            ]
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)

    session = configured_agent.create_session("s_stage", working_directory=str(tmp_path))
    events = []
    async for evt in configured_agent.process_input_stream(
        user_input="oi", session_id=session.session_id
    ):
        events.append(evt)

    # All STAGE events must precede the first non-STAGE content event.
    types = [e.type for e in events]
    first_content_idx = next(
        (i for i, t in enumerate(types) if t is not StreamEventType.STAGE), None
    )
    assert first_content_idx is not None and first_content_idx > 0, (
        "expected at least one STAGE event before the first content event"
    )
    pre_content_types = types[:first_content_idx]
    assert all(t is StreamEventType.STAGE for t in pre_content_types), (
        f"non-STAGE events found before first content: {pre_content_types}"
    )

    # Stage labels are non-empty and human-readable.
    stage_labels = [e.stage for e in events if e.type is StreamEventType.STAGE]
    assert all(isinstance(s, str) and s.strip() for s in stage_labels)


@pytest.mark.asyncio
async def test_slash_command_yields_single_text(configured_agent, tmp_path: Path):
    """Slash commands degrade to a single TEXT_DELTA + USAGE_FINAL — no streaming round-trip."""
    from deile.core.agent import AgentResponse, AgentStatus

    response = AgentResponse(
        content="help text", status=AgentStatus.IDLE, execution_time=0.01,
    )
    configured_agent._process_slash_command = AsyncMock(return_value=response)
    session = configured_agent.create_session("s2", working_directory=str(tmp_path))

    events = []
    async for evt in configured_agent.process_input_stream(
        user_input="/help", session_id=session.session_id
    ):
        events.append(evt)
    types = [e.type for e in events]
    assert types == [StreamEventType.STAGE, StreamEventType.TEXT_DELTA, StreamEventType.USAGE_FINAL]
    assert events[1].text == "help text"


@pytest.mark.asyncio
async def test_slash_command_with_rich_renderable_content(configured_agent, tmp_path: Path):
    """When a slash command returns a Rich renderable (e.g. ``/model list``
    returns a ``Table``), the streaming pipeline must forward the
    renderable verbatim through a RICH_RENDERABLE event so the renderer
    can let Rich's own width-aware layout run at the actual terminal
    width. Flattening to text and yielding TEXT_DELTA is what made the
    table re-render through ``Markdown()`` and shatter into scattered
    pipes at the terminal width.
    """
    from deile.core.agent import AgentResponse, AgentStatus
    from rich.table import Table

    table = Table(title="Models")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_row("openai", "gpt-4o")
    table.add_row("anthropic", "claude-opus-4-7")

    response = AgentResponse(
        content=table, status=AgentStatus.IDLE, execution_time=0.01,
    )
    configured_agent._process_slash_command = AsyncMock(return_value=response)
    session = configured_agent.create_session("s_rich", working_directory=str(tmp_path))

    events = []
    async for evt in configured_agent.process_input_stream(
        user_input="/model list", session_id=session.session_id
    ):
        events.append(evt)
    types = [e.type for e in events]
    assert types == [StreamEventType.STAGE, StreamEventType.RICH_RENDERABLE, StreamEventType.USAGE_FINAL]
    # The original Table must arrive verbatim — same identity, no text
    # round-trip — so Rich can re-flow columns at the actual terminal width.
    assert events[1].renderable is table
    assert events[1].text is None


@pytest.mark.asyncio
async def test_autonomous_path_yields_single_text(configured_agent, tmp_path: Path):
    configured_agent.process_autonomous_request = AsyncMock(return_value="autonomous reply")
    session = configured_agent.create_session("s3", working_directory=str(tmp_path))

    events = [
        e
        async for e in configured_agent.process_input_stream(
            user_input="do thing", session_id=session.session_id
        )
    ]
    types = [e.type for e in events]
    assert types == [StreamEventType.STAGE, StreamEventType.TEXT_DELTA, StreamEventType.USAGE_FINAL]
    assert events[1].text == "autonomous reply"


@pytest.mark.asyncio
async def test_error_in_setup_emits_error_event(configured_agent, tmp_path: Path):
    """Exception during stream setup → single ERROR event, not a crash."""

    async def _boom(*a, **kw):
        raise RuntimeError("setup failed")

    configured_agent.context_manager.build_context = AsyncMock(side_effect=RuntimeError("setup failed"))
    session = configured_agent.create_session("s4", working_directory=str(tmp_path))

    events = [
        e
        async for e in configured_agent.process_input_stream(
            user_input="hi", session_id=session.session_id
        )
    ]
    assert events[-1].type == StreamEventType.ERROR
    env = events[-1].error_envelope
    assert env["error_type"] == "RuntimeError"
    assert "setup failed" in env["message"]


# ---------------------------------------------------------------------------
# BG-001 regression: streaming must preserve ToolResult.metadata so the
# validation gate can still detect post-write writes. Before the fix, the
# metadata round-trip lost flags like ``post_write_validation_required`` and
# the gate became silently inert in streaming mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_preserves_tool_result_metadata_through_gate(
    configured_agent, tmp_path: Path
):
    """The reconstructed ToolResult handed to ``_apply_validation_gate`` must
    carry the original ``post_write_validation_required`` flag set by the tool
    (e.g. write_file). If this regresses, the post-write gate goes silent.
    """
    from deile.tools.base import ToolResult, ToolStatus

    captured: Dict[str, Any] = {}

    async def _capturing_gate(*, user_input, parse_result, session, content, tool_results):
        captured["tool_results"] = list(tool_results)
        return content, tool_results

    configured_agent._apply_validation_gate = _capturing_gate

    # tool_registry.execute_tool returns a ToolResult flagged as needing
    # post-write validation — exactly what file_tools.write_file emits when
    # writing an executable file.
    flagged = ToolResult(
        status=ToolStatus.SUCCESS,
        message="wrote main.py",
        data="main.py",
        metadata={
            "post_write_validation_required": True,
            "post_write_validation_command": "python -m py_compile main.py",
            "file_path": "main.py",
            "function_name": "write_file",
        },
    )
    configured_agent.tool_registry.execute_tool = AsyncMock(return_value=flagged)

    fake = _FakeProvider(
        iterations=[
            # iteration 0: model requests write_file
            [
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_START,
                    tool_call_id="call_1",
                    tool_name="write_file",
                ),
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id="call_1",
                    tool_name="write_file",
                    arguments={"path": "main.py", "content": "print('x')"},
                ),
            ],
            # iteration 1: model produces final answer (no more tool calls)
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="done"),
            ],
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)

    session = configured_agent.create_session(
        "s_meta", working_directory=str(tmp_path)
    )
    async for _ in configured_agent.process_input_stream(
        user_input="write main.py", session_id=session.session_id
    ):
        pass

    assert "tool_results" in captured, "validation gate was never invoked"
    tool_results = captured["tool_results"]
    assert len(tool_results) == 1, f"expected 1 collected tool result, got {len(tool_results)}"
    md = tool_results[0].metadata
    assert md.get("post_write_validation_required") is True, (
        "BG-001 regression: post_write_validation_required lost on the "
        "stream→reconstruct path; the validation gate would be silently disabled"
    )
    assert md.get("post_write_validation_command") == "python -m py_compile main.py"
    assert md.get("file_path") == "main.py"
    # Stream-only fields should still be present
    assert md.get("function_name") == "write_file"
    assert md.get("tool_call_id") == "call_1"


@pytest.mark.asyncio
async def test_streaming_gate_fires_on_unvalidated_write(
    configured_agent, tmp_path: Path
):
    """End-to-end: a streamed write_file with the validation flag, no validation
    tool afterwards, must trigger the real gate and produce a TEXT_DELTA event
    tagged ``source='validation_gate'``. This is the contract that the streaming
    path was silently breaking before BG-001 was fixed.
    """
    from deile.tools.base import ToolResult, ToolStatus

    flagged = ToolResult(
        status=ToolStatus.SUCCESS,
        message="wrote evil.py",
        data="evil.py",
        metadata={
            "post_write_validation_required": True,
            "post_write_validation_command": "python -m py_compile evil.py",
            "file_path": "evil.py",
            "function_name": "write_file",
        },
    )
    configured_agent.tool_registry.execute_tool = AsyncMock(return_value=flagged)

    # Use the REAL gate (drop the no-op stub from the fixture) and stub only
    # the retry path — that's what the gate calls when it fires.
    from deile.core.agent import DeileAgent
    configured_agent._apply_validation_gate = DeileAgent._apply_validation_gate.__get__(
        configured_agent, type(configured_agent)
    )
    configured_agent._process_iterative_function_calling = AsyncMock(
        return_value=("validation gate retry reply", [])
    )

    fake = _FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_START,
                    tool_call_id="call_evil",
                    tool_name="write_file",
                ),
                UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id="call_evil",
                    tool_name="write_file",
                    arguments={"path": "evil.py", "content": "import os"},
                ),
            ],
            [UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="ok done")],
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)

    session = configured_agent.create_session(
        "s_gate", working_directory=str(tmp_path)
    )

    events = [
        e
        async for e in configured_agent.process_input_stream(
            user_input="write evil.py", session_id=session.session_id
        )
    ]

    # Gate must have fired exactly once
    assert configured_agent._process_iterative_function_calling.await_count == 1, (
        "validation gate did not fire on an unvalidated executable write — "
        "BG-001 has regressed"
    )

    # And its retry text must have been emitted as a validation_gate-tagged delta
    gate_deltas = [
        e
        for e in events
        if e.type is StreamEventType.TEXT_DELTA and e.source == "validation_gate"
    ]
    assert len(gate_deltas) == 1, (
        f"expected exactly one validation_gate TEXT_DELTA, got {len(gate_deltas)}"
    )
    # The delta carries a one-line marker (rendered inside the yellow panel)
    # making it explicit that the corrected reply REPLACES the prior streamed
    # response, followed by the gate's standalone retry reply.
    assert gate_deltas[0].text == (
        "(corrected reply — replaces the response above)\n\n"
        "validation gate retry reply"
    )


@pytest.mark.asyncio
async def test_streaming_gate_does_not_see_proactive_results(
    configured_agent, tmp_path: Path
):
    """BG-002: proactive_results must NOT be passed to the gate, otherwise a
    proactive bash_execute could falsely satisfy the post-write validation
    requirement of a write the model issued during the streamed turn.
    """
    from deile.tools.base import ToolResult, ToolStatus

    captured: Dict[str, Any] = {}

    async def _capturing_gate(*, user_input, parse_result, session, content, tool_results):
        captured["tool_results"] = list(tool_results)
        return content, tool_results

    configured_agent._apply_validation_gate = _capturing_gate

    proactive_tr = ToolResult(
        status=ToolStatus.SUCCESS,
        message="proactive ran",
        data=None,
        metadata={"function_name": "bash_execute"},
    )
    configured_agent._execute_proactive_tools = AsyncMock(return_value=[proactive_tr])

    fake = _FakeProvider(
        iterations=[
            [UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hi")],
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)

    session = configured_agent.create_session(
        "s_proactive", working_directory=str(tmp_path)
    )
    async for _ in configured_agent.process_input_stream(
        user_input="hello", session_id=session.session_id
    ):
        pass

    seen = captured.get("tool_results", [])
    assert all(
        tr.metadata.get("function_name") != "bash_execute" for tr in seen
    ), "proactive_results leaked into the streaming validation gate input"
