"""Tests para ``LocalSubAgentRunner`` (issue #257).

Garante que o runner local:
  * Atualiza ``SubAgentState`` ao consumir o stream do agente.
  * Mapeia eventos ``UnifiedStreamEvent`` corretamente
    (TOOL_USE_END → TOOL, TOOL_RESULT → TOOL_RESULT, TEXT_DELTA → TEXT).
  * Registra arquivos tocados via metadata da tool.
  * Captura exceções (não propaga) e marca ``status="error"``.
  * Cada sub-tarefa recebe um ``session_id`` único.
"""
from __future__ import annotations

import pytest

from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.orchestration.subagents.events import (SubAgentEventKind,
                                                  SubAgentState, SubAgentTask)
from deile.orchestration.subagents.runner import LocalSubAgentRunner

pytestmark = pytest.mark.unit


class _StubAgent:
    """DeileAgent stub que devolve um stream pré-definido de eventos."""

    def __init__(self, events: list[UnifiedStreamEvent], *, raise_exc=None):
        self._events = events
        self._raise = raise_exc
        self.last_session_id: str | None = None
        self.last_kwargs: dict = {}
        self.call_count = 0

    def process_input_stream(self, prompt: str, **kwargs):
        self.call_count += 1
        self.last_session_id = kwargs.get("session_id")
        self.last_kwargs = dict(kwargs)
        events_ref = self._events
        raise_ref = self._raise

        async def _gen():
            for evt in events_ref:
                yield evt
            if raise_ref is not None:
                raise raise_ref

        return _gen()


def _task(index=1, persona=None, model=None) -> SubAgentTask:
    return SubAgentTask(
        index=index,
        description=f"task #{index}",
        prompt="prompt longo o suficiente para passar do mínimo defensivo",
        persona=persona,
        model=model,
    )


async def test_maps_text_delta_and_tool_lifecycle_to_state_and_events():
    events_in = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Lendo arquivos…"),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="read_file",
            arguments={"file_path": "deile/x.py"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="read_file",
            tool_status="success",
            tool_result_summary="500 bytes lidos",
            tool_metadata={"file_path": "deile/x.py"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t2",
            tool_name="write_file",
            arguments={"file_path": "deile/y.py", "content": "..."},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t2",
            tool_name="write_file",
            tool_status="success",
            tool_result_summary="ok",
            tool_metadata={"file_path": "deile/y.py"},
        ),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="\n\nPronto."),
    ]
    agent = _StubAgent(events_in)
    runner = LocalSubAgentRunner(agent)
    state = SubAgentState(task=_task())

    captured: list = []
    runner_evt = lambda e: captured.append(e)  # noqa: E731

    await runner.run_one(state, on_event=runner_evt)

    assert state.status == "ok"
    assert state.error is None
    assert state.result_text.strip().endswith("Pronto.")
    # files_touched coleta via metadata da tool OU via primary arg (write_file).
    assert "deile/x.py" in state.files_touched
    assert "deile/y.py" in state.files_touched
    # progress_lines deve ter referências a read_file + write_file
    joined = " | ".join(state.progress_lines)
    assert "read_file" in joined
    assert "write_file" in joined
    # Eventos emitidos cobrem o ciclo.
    kinds = [e.kind for e in captured]
    assert SubAgentEventKind.STARTED in kinds
    assert SubAgentEventKind.TOOL in kinds
    assert SubAgentEventKind.TOOL_RESULT in kinds
    assert SubAgentEventKind.COMPLETED in kinds


async def test_exception_caught_and_marked_error_without_propagating():
    events_in = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="começando"),
    ]
    agent = _StubAgent(events_in, raise_exc=RuntimeError("kaboom"))
    runner = LocalSubAgentRunner(agent)
    state = SubAgentState(task=_task(index=2))

    captured: list = []

    # Runner SE COMPROMETE a não propagar — orquestrador depende disso.
    await runner.run_one(state, on_event=captured.append)

    assert state.status == "error"
    assert state.error is not None and "kaboom" in state.error
    assert any(e.kind is SubAgentEventKind.FAILED for e in captured)


async def test_clean_session_id_per_subagent_and_kwargs_propagation():
    agent = _StubAgent([])
    runner = LocalSubAgentRunner(agent)
    state = SubAgentState(task=_task(index=3, persona="architect", model="claude-opus"))

    await runner.run_one(state, on_event=lambda _: None)

    assert agent.last_session_id is not None
    assert agent.last_session_id.startswith("subagent_3_")
    # persona vira persona_name nos kwargs do agente.
    assert agent.last_kwargs.get("persona_name") == "architect"
    assert agent.last_kwargs.get("forced_model") == "claude-opus"


async def test_prompt_envelope_isolates_subagent_context():
    """Sub-DEILE recebe envelope explícito de 'CONTEXTO' + 'TAREFA' antes do
    prompt do usuário. Sem isso, o LLM pode tentar interagir com 'o usuário'
    ou re-narrar o estado da conversa principal (que não existe pra ele).
    """
    captured_prompt = []

    class _CapturingAgent:
        def process_input_stream(self, prompt, **kwargs):
            captured_prompt.append(prompt)
            async def _gen():
                return
                yield  # pragma: no cover — generator vazio
            return _gen()

    runner = LocalSubAgentRunner(_CapturingAgent())
    state = SubAgentState(task=_task(index=1))
    await runner.run_one(state, on_event=lambda _: None)

    assert len(captured_prompt) == 1
    body = captured_prompt[0]
    assert "[CONTEXTO]" in body
    assert "sub-DEILE" in body or "sub-DEILE" in body  # case-sensitive
    assert "[TAREFA]" in body
    # Prompt original do usuário deve estar lá depois do header.
    assert "prompt longo o suficiente" in body


async def test_two_subagents_get_distinct_session_ids():
    agent = _StubAgent([])
    runner = LocalSubAgentRunner(agent)
    st1 = SubAgentState(task=_task(index=1))
    st2 = SubAgentState(task=_task(index=2))

    await runner.run_one(st1, on_event=lambda _: None)
    sid1 = agent.last_session_id
    await runner.run_one(st2, on_event=lambda _: None)
    sid2 = agent.last_session_id

    assert sid1 and sid2 and sid1 != sid2
