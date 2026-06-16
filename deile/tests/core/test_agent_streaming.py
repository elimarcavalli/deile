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
async def test_text_segments_around_tool_calls_get_paragraph_separator(
    configured_agent, tmp_path: Path
):
    """Issue #257 round 5: TEXT_DELTAs ANTES e DEPOIS de TOOL_USE_END devem
    ser separados por ``\\n\\n`` no histórico — sem isso, frases ficavam
    coladas em ``/resume`` (`"Vou ler.Pronto."` em vez de
    `"Vou ler.\\n\\nPronto."`).

    Patch direto em ``_stream_chat_with_tools`` para evitar invocar o
    tool_loop_executor real (que exige tool registry funcional).
    """

    async def _fake_stream(*args, **kwargs):
        yield UnifiedStreamEvent(
            type=StreamEventType.TEXT_DELTA, text="Vou ler o arquivo."
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="read_file",
            arguments={"file_path": "x.py"},
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="read_file",
            tool_status="success",
            tool_result_summary="ok",
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.TEXT_DELTA, text="Pronto, conferido."
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        )

    configured_agent._stream_chat_with_tools = _fake_stream
    session = configured_agent.create_session("seg", working_directory=str(tmp_path))

    async for _ in configured_agent.process_input_stream(
        user_input="hi", session_id=session.session_id
    ):
        pass

    assistant_entries = [
        e for e in session.conversation_history if e["role"] == "assistant"
    ]
    assert assistant_entries, "no assistant entry persisted"
    content = assistant_entries[-1]["content"]
    # Fronteira do tool inserida como \n\n
    assert "Vou ler o arquivo." in content
    assert "Pronto, conferido." in content
    # Texto NÃO pode estar grudado
    assert "Vou ler o arquivo.Pronto" not in content
    assert "\n\n" in content


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
async def test_stage_events_emitted_before_first_content(
    configured_agent, tmp_path: Path
):
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

    session = configured_agent.create_session(
        "s_stage", working_directory=str(tmp_path)
    )
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
    assert (
        first_content_idx is not None and first_content_idx > 0
    ), "expected at least one STAGE event before the first content event"
    pre_content_types = types[:first_content_idx]
    assert all(
        t is StreamEventType.STAGE for t in pre_content_types
    ), f"non-STAGE events found before first content: {pre_content_types}"

    # Stage labels are non-empty and human-readable.
    stage_labels = [e.stage for e in events if e.type is StreamEventType.STAGE]
    assert all(isinstance(s, str) and s.strip() for s in stage_labels)


@pytest.mark.asyncio
async def test_slash_command_yields_single_text(configured_agent, tmp_path: Path):
    """Slash commands degrade to a single TEXT_DELTA + USAGE_FINAL — no streaming round-trip."""
    from deile.core.agent import AgentResponse, AgentStatus

    response = AgentResponse(
        content="help text",
        status=AgentStatus.IDLE,
        execution_time=0.01,
    )
    configured_agent._process_slash_command = AsyncMock(return_value=response)
    session = configured_agent.create_session("s2", working_directory=str(tmp_path))

    events = []
    async for evt in configured_agent.process_input_stream(
        user_input="/help", session_id=session.session_id
    ):
        events.append(evt)
    types = [e.type for e in events]
    assert types == [
        StreamEventType.STAGE,
        StreamEventType.TEXT_DELTA,
        StreamEventType.USAGE_FINAL,
    ]
    assert events[1].text == "help text"


@pytest.mark.asyncio
async def test_slash_command_with_rich_renderable_content(
    configured_agent, tmp_path: Path
):
    """When a slash command returns a Rich renderable (e.g. ``/model list``
    returns a ``Table``), the streaming pipeline must forward the
    renderable verbatim through a RICH_RENDERABLE event so the renderer
    can let Rich's own width-aware layout run at the actual terminal
    width. Flattening to text and yielding TEXT_DELTA is what made the
    table re-render through ``Markdown()`` and shatter into scattered
    pipes at the terminal width.
    """
    from rich.table import Table

    from deile.core.agent import AgentResponse, AgentStatus

    table = Table(title="Models")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_row("openai", "gpt-4o")
    table.add_row("anthropic", "claude-opus-4-8")

    response = AgentResponse(
        content=table,
        status=AgentStatus.IDLE,
        execution_time=0.01,
    )
    configured_agent._process_slash_command = AsyncMock(return_value=response)
    session = configured_agent.create_session("s_rich", working_directory=str(tmp_path))

    events = []
    async for evt in configured_agent.process_input_stream(
        user_input="/model list", session_id=session.session_id
    ):
        events.append(evt)
    types = [e.type for e in events]
    assert types == [
        StreamEventType.STAGE,
        StreamEventType.RICH_RENDERABLE,
        StreamEventType.USAGE_FINAL,
    ]
    # The original Table must arrive verbatim — same identity, no text
    # round-trip — so Rich can re-flow columns at the actual terminal width.
    assert events[1].renderable is table
    assert events[1].text is None


@pytest.mark.asyncio
async def test_autonomous_path_yields_single_text(configured_agent, tmp_path: Path):
    configured_agent.process_autonomous_request = AsyncMock(
        return_value="autonomous reply"
    )
    session = configured_agent.create_session("s3", working_directory=str(tmp_path))

    events = [
        e
        async for e in configured_agent.process_input_stream(
            user_input="do thing", session_id=session.session_id
        )
    ]
    types = [e.type for e in events]
    assert types == [
        StreamEventType.STAGE,
        StreamEventType.TEXT_DELTA,
        StreamEventType.USAGE_FINAL,
    ]
    assert events[1].text == "autonomous reply"


@pytest.mark.asyncio
async def test_skip_autonomous_kwarg_also_skips_workflow_path(
    configured_agent, tmp_path: Path
):
    """Issue #257 round 4: ``_skip_autonomous=True`` deve pular tanto o
    autonomous path quanto o workflow path. O workflow path executa tools
    OPACAMENTE (yieldando só TEXT_DELTA agregado), deixando o painel
    multipanel sem atividade visível mesmo com sub-DEILE trabalhando.

    Confirmado em teste end-to-end real (issue #257 round 4): sub-DEILEs
    entravam no workflow path e o painel ficava '(sem atividade ainda)'
    enquanto write_file/bash_execute rodavam internamente.
    """
    # Workflow seria criado normalmente
    configured_agent._should_create_workflow = AsyncMock(return_value=True)
    configured_agent.process_autonomous_request = AsyncMock(return_value=None)
    fake = _FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(
                    type=StreamEventType.TEXT_DELTA, text="tool path used"
                ),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
                ),
            ]
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)
    session = configured_agent.create_session(
        "skip_wf", working_directory=str(tmp_path)
    )

    # Consome o stream sem inspecionar eventos — só importa que
    # ``_should_create_workflow`` NÃO seja invocado quando ``_skip_autonomous``.
    async for _ in configured_agent.process_input_stream(
        user_input="multi-step task",
        session_id=session.session_id,
        _skip_autonomous=True,
    ):
        pass
    configured_agent._should_create_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_skip_autonomous_kwarg_bypasses_autonomous_path(
    configured_agent, tmp_path: Path
):
    """Issue #257 round 4: ``_skip_autonomous=True`` deve fazer
    ``process_input_stream`` pular o caminho ``process_autonomous_request``
    para ir direto ao tool-loop. Necessário para sub-DEILEs alimentarem o
    painel multipanel com eventos TOOL_USE_END/TOOL_RESULT."""
    # Configura autonomous para retornar resposta — mas o skip deve ignorar.
    configured_agent.process_autonomous_request = AsyncMock(
        return_value="should NOT be used"
    )
    # Fake provider para a chat-with-tools path
    fake = _FakeProvider(
        iterations=[
            [
                UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="tool path"),
                UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
                ),
            ]
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)
    session = configured_agent.create_session(
        "skip_auto", working_directory=str(tmp_path)
    )

    events = [
        e
        async for e in configured_agent.process_input_stream(
            user_input="hi",
            session_id=session.session_id,
            _skip_autonomous=True,
        )
    ]
    texts = [e.text for e in events if e.type is StreamEventType.TEXT_DELTA and e.text]
    assert "should NOT be used" not in "".join(
        texts
    ), "autonomous result leaked into stream despite _skip_autonomous=True"
    # process_autonomous_request NÃO deve ter sido chamado
    configured_agent.process_autonomous_request.assert_not_called()


@pytest.mark.asyncio
async def test_error_in_setup_emits_error_event(configured_agent, tmp_path: Path):
    """Exception during stream setup → single ERROR event, not a crash."""

    async def _boom(*a, **kw):
        raise RuntimeError("setup failed")

    configured_agent.context_manager.build_context = AsyncMock(
        side_effect=RuntimeError("setup failed")
    )
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

    async def _capturing_gate(
        *, user_input, parse_result, session, content, tool_results
    ):
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

    session = configured_agent.create_session("s_meta", working_directory=str(tmp_path))
    async for _ in configured_agent.process_input_stream(
        user_input="write main.py", session_id=session.session_id
    ):
        pass

    assert "tool_results" in captured, "validation gate was never invoked"
    tool_results = captured["tool_results"]
    assert (
        len(tool_results) == 1
    ), f"expected 1 collected tool result, got {len(tool_results)}"
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

    session = configured_agent.create_session("s_gate", working_directory=str(tmp_path))

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
    assert (
        len(gate_deltas) == 1
    ), f"expected exactly one validation_gate TEXT_DELTA, got {len(gate_deltas)}"
    # The delta carries a one-line marker (rendered inside the yellow panel)
    # making it explicit that the corrected reply REPLACES the prior streamed
    # response, followed by the gate's standalone retry reply. We assert on
    # the structural shape (marker substring + retry text), not the exact
    # marker wording — copy is allowed to evolve.
    assert "SUBSTITUI" in gate_deltas[0].text
    assert "validação real" in gate_deltas[0].text
    assert gate_deltas[0].text.endswith("validation gate retry reply")


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

    async def _capturing_gate(
        *, user_input, parse_result, session, content, tool_results
    ):
        captured["tool_results"] = list(tool_results)
        return content, tool_results

    configured_agent._apply_validation_gate = _capturing_gate

    proactive_tr = ToolResult(
        status=ToolStatus.SUCCESS,
        message="proactive ran",
        data=None,
        metadata={"function_name": "bash_execute"},
    )

    async def _fake_proactive_stream(_user_input, _session):
        # Stream-shaped stub: emit synthetic events + final results sentinel.
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="pr-test",
            tool_name="proactive:bash_execute",
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="pr-test",
            tool_name="proactive:bash_execute",
            tool_status="success",
            tool_result_summary="ok",
            tool_metadata={
                "function_name": "bash_execute",
                "proactive_execution": True,
            },
        )
        yield ("results", [proactive_tr])

    configured_agent._execute_proactive_tools_stream = _fake_proactive_stream
    # Keep the non-streaming alias coherent (it's the wrapper around the stream).
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


@pytest.mark.asyncio
async def test_proactive_tool_events_are_yielded_to_caller(
    configured_agent, tmp_path: Path
):
    """Proactive tools são visíveis no transcript: o stream yield-a
    TOOL_USE_START/END/RESULT marcados como ``proactive:<tool>`` antes
    do primeiro TEXT_DELTA do LLM.
    """
    from deile.tools.base import ToolResult, ToolStatus

    async def _fake_proactive_stream(_user_input, _session):
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="pr-0",
            tool_name="proactive:read_file",
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="pr-0",
            tool_name="proactive:read_file",
            arguments={"file_path": "foo.py"},
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="pr-0",
            tool_name="proactive:read_file",
            tool_status="success",
            tool_result_summary="ok",
            tool_metadata={"function_name": "read_file", "proactive_execution": True},
        )
        yield (
            "results",
            [
                ToolResult(
                    status=ToolStatus.SUCCESS,
                    message="ok",
                    metadata={
                        "function_name": "read_file",
                        "proactive_execution": True,
                    },
                )
            ],
        )

    configured_agent._execute_proactive_tools_stream = _fake_proactive_stream

    fake = _FakeProvider(
        iterations=[
            [UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="ok")],
        ]
    )
    configured_agent.model_router.select_provider = AsyncMock(return_value=fake)

    session = configured_agent.create_session(
        "s_proactive_vis", working_directory=str(tmp_path)
    )

    collected: List[UnifiedStreamEvent] = []
    async for event in configured_agent.process_input_stream(
        user_input="hello", session_id=session.session_id
    ):
        collected.append(event)

    # Assert the proactive lifecycle events show up in order, BEFORE the LLM's
    # TEXT_DELTA — i.e. the user actually sees them while they execute.
    types_and_names = [(e.type, e.tool_name) for e in collected]
    pr_start_idx = next(
        i
        for i, (t, n) in enumerate(types_and_names)
        if t is StreamEventType.TOOL_USE_START and n == "proactive:read_file"
    )
    pr_result_idx = next(
        i
        for i, (t, n) in enumerate(types_and_names)
        if t is StreamEventType.TOOL_RESULT and n == "proactive:read_file"
    )
    first_text_idx = next(
        (
            i
            for i, e in enumerate(collected)
            if e.type is StreamEventType.TEXT_DELTA and e.text == "ok"
        ),
        None,
    )
    assert pr_start_idx < pr_result_idx, "TOOL_RESULT before TOOL_USE_START"
    assert (
        first_text_idx is None or pr_result_idx < first_text_idx
    ), "proactive events must surface BEFORE the LLM's first text delta"


@pytest.mark.asyncio
async def test_real_proactive_stream_imports_resolve_when_invoked(configured_agent):
    """Smoke: the REAL ``_execute_proactive_tools_stream`` resolves all its
    inner imports when iterated. Guards against a regression where the
    function used ``UnifiedStreamEvent`` / ``StreamEventType`` without
    importing them — ruff/lint catches it, but if a future refactor moves
    the import out again, this test fails fast.

    Other tests in this file monkey-patch the function with a fake stream,
    which masks NameErrors in the real body. This test does NOT patch.
    """
    # Sanity: fixture sets proactive_analyzer = None, so the body hits the
    # early-return after the imports. That alone proves the imports resolve.
    configured_agent.proactive_analyzer = None
    fake_session = MagicMock()
    fake_session.working_directory = "."
    fake_session.context_data = {}

    items = []
    async for item in configured_agent._execute_proactive_tools_stream(
        "hello", fake_session
    ):
        items.append(item)

    # Generator must yield exactly the results sentinel — empty list because
    # the analyzer is None.
    assert items == [("results", [])]
