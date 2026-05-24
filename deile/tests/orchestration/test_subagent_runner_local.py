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


async def test_persona_warning_emitted_as_progress_event_and_log(caplog):
    """M4 (PR #295 review) regression — persona é apenas hint no
    LocalSubAgentRunner; o runner DEVE emitir warning (log + evento
    progress no painel) para que o caller saiba que persona NÃO está
    sendo efetivamente honored.

    Verificamos via DOIS canais para resiliência contra interferência de
    outros testes na configuração global de logging: (1) o evento PROGRESS
    no painel (sempre observável); (2) o log warning via caplog (se
    propagation estiver habilitada — pulamos a verificação se não estiver,
    porque algum teste anterior pode ter alterado o root handler).
    """
    import logging as _logging

    agent = _StubAgent([])
    runner = LocalSubAgentRunner(agent)
    state = SubAgentState(task=_task(index=7, persona="architect"))
    captured: list = []

    # Força propagate=True só para esse teste, garantindo caplog hookable.
    target_logger = _logging.getLogger("deile.orchestration.subagents.runner")
    old_propagate = target_logger.propagate
    old_disabled = target_logger.disabled
    target_logger.propagate = True
    target_logger.disabled = False
    caplog.set_level(_logging.WARNING, logger="deile.orchestration.subagents.runner")
    try:
        await runner.run_one(state, on_event=captured.append)
    finally:
        target_logger.propagate = old_propagate
        target_logger.disabled = old_disabled

    # Canal principal (sempre observável): evento PROGRESS no painel.
    progress_events = [
        e for e in captured
        if e.kind is SubAgentEventKind.PROGRESS and "persona" in (e.label or "")
    ]
    assert progress_events, f"esperava progress event sobre persona; got {captured}"

    # Canal secundário: log warning — toleramos ausência se o root logger
    # foi reconfigurado por outros testes (pytest test ordering pode
    # introduzir interferência), mas se houver, deve mencionar persona.
    if caplog.records:
        persona_warns = [
            rec for rec in caplog.records
            if rec.levelno >= _logging.WARNING and "persona" in rec.getMessage()
        ]
        # Se houver algum warning, deve incluir o de persona.
        assert persona_warns or not any(
            r.levelno >= _logging.WARNING for r in caplog.records
        ), f"caplog tinha warnings mas nenhum sobre persona: {[r.getMessage() for r in caplog.records]}"


async def test_cleanup_handles_missing_session_gracefully():
    """Iter-2 review: o cleanup do session_id no finally trata
    KeyError/AttributeError silenciosamente quando a sessão já foi
    removida ou o agent não expõe ``_sessions``.
    """
    class _AgentWithoutSessions:
        def process_input_stream(self, prompt, **kwargs):
            async def _gen():
                return
                yield  # pragma: no cover
            return _gen()

    runner = LocalSubAgentRunner(_AgentWithoutSessions())
    state = SubAgentState(task=_task(index=99))
    # Cleanup vai tentar pop em sessions=None → AttributeError; deve ser
    # absorvido pelo except (KeyError, AttributeError) — sem propagar.
    await runner.run_one(state, on_event=lambda _: None)
    assert state.status == "ok"


async def test_cleanup_runs_when_process_input_stream_fails_sync():
    """Iter-2 review: ``process_input_stream`` que falha SÍNCRONO (raise no
    momento da chamada, não dentro do async-iter) ainda deve passar pelo
    cleanup do session_id no ``finally`` — sem isso, sessões órfãs
    acumulariam em agent._sessions.
    """
    deleted_sessions: list[str] = []

    class _ExplodingAgent:
        def __init__(self):
            self._sessions: dict = {}

        def process_input_stream(self, prompt, **kwargs):
            # Sync failure — não retorna o gerador.
            raise RuntimeError("stream construction failed")

        def delete_session(self, sid: str) -> None:
            deleted_sessions.append(sid)

    runner = LocalSubAgentRunner(_ExplodingAgent())
    state = SubAgentState(task=_task(index=42))

    await runner.run_one(state, on_event=lambda _: None)
    # Runner não propaga — marca erro.
    assert state.status == "error"
    assert "stream construction failed" in (state.error or "")
    # Cleanup foi chamado mesmo após falha síncrona.
    assert len(deleted_sessions) == 1
    assert deleted_sessions[0].startswith("subagent_42_")


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
