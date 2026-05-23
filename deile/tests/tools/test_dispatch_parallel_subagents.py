"""Tests para a tool ``dispatch_parallel_subagents`` (issue #257).

Foca em:
  * Validação de schema (2-5 subtasks, descriptions únicas, prompts dentro
    dos limites de tamanho, personas no enum).
  * Cooldown anti-loop por session_id.
  * Fallback claro quando ``_agent`` não está em session_data.
  * Caminho feliz: chama o orquestrador e devolve resumo consolidado.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from deile.orchestration.subagents.events import (SubAgentState, SubAgentTask)
from deile.tools.base import ToolContext
from deile.tools.dispatch_parallel_subagents import (
    DispatchParallelSubagentsTool, _build_tasks_from_payload)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- validation


def _valid_subtask(i: int = 1, persona=None, model=None) -> dict:
    return {
        "description": f"refator módulo #{i}",
        "prompt": "Refator detalhado do módulo X para guard clauses, "
                  "extraindo validações para o topo das funções.",
        **({"persona": persona} if persona else {}),
        **({"model": model} if model else {}),
    }


def test_build_tasks_accepts_minimum_two():
    tasks, err = _build_tasks_from_payload([_valid_subtask(1), _valid_subtask(2)])
    assert err is None
    assert [t.index for t in tasks] == [1, 2]


def test_build_tasks_rejects_single_subtask():
    _, err = _build_tasks_from_payload([_valid_subtask(1)])
    assert err is not None and "between" in err


def test_build_tasks_rejects_six_subtasks():
    payload = [_valid_subtask(i) for i in range(1, 7)]
    _, err = _build_tasks_from_payload(payload)
    assert err is not None and "between" in err


def test_build_tasks_rejects_too_short_prompt():
    short = {"description": "refator", "prompt": "fix it"}
    _, err = _build_tasks_from_payload([short, _valid_subtask(2)])
    assert err is not None and "too short" in err


def test_build_tasks_rejects_duplicate_descriptions():
    a = _valid_subtask(1)
    b = _valid_subtask(1)  # same description
    _, err = _build_tasks_from_payload([a, b])
    assert err is not None and "duplicates" in err


def test_build_tasks_rejects_invalid_persona():
    bad = _valid_subtask(1)
    bad["persona"] = "evil-persona"
    _, err = _build_tasks_from_payload([bad, _valid_subtask(2)])
    assert err is not None and "persona" in err


def test_build_tasks_rejects_oversized_description():
    long = _valid_subtask(1)
    long["description"] = "x" * 120
    _, err = _build_tasks_from_payload([long, _valid_subtask(2)])
    assert err is not None and "description" in err


def test_build_tasks_rejects_non_object_item():
    _, err = _build_tasks_from_payload(["not an object", _valid_subtask(2)])
    assert err is not None


def test_build_tasks_rejects_non_list():
    _, err = _build_tasks_from_payload("not a list")
    assert err is not None


# --------------------------------------------------------------------- tool


@pytest.fixture(autouse=True)
def _isolate_cooldown():
    DispatchParallelSubagentsTool._LAST_DISPATCH.clear()
    DispatchParallelSubagentsTool._SESSION_LOCKS.clear()
    yield
    DispatchParallelSubagentsTool._LAST_DISPATCH.clear()
    DispatchParallelSubagentsTool._SESSION_LOCKS.clear()


async def test_tool_rejects_payload_without_agent_in_session_data():
    tool = DispatchParallelSubagentsTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "test"},
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "AGENT_NOT_AVAILABLE"


async def test_tool_validation_error_propagates_as_bad_request():
    tool = DispatchParallelSubagentsTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1)]},  # < 2
        session_data={"session_id": "test", "_agent": object()},
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "BAD_REQUEST"


async def test_tool_cooldown_kicks_in_on_second_immediate_call(monkeypatch):
    """1ª chamada registra o slot; 2ª chamada imediata trava no cooldown.

    Patcheia o orquestrador pra evitar exercitar o runner real (que tentaria
    chamar ``process_input_stream`` num ``object()``). O foco do teste é
    a interação cooldown-tool.
    """
    tool = DispatchParallelSubagentsTool()

    class _NoopOrch:
        def __init__(self, *a, **kw):
            pass

        async def run(self, tasks):
            from deile.orchestration.subagents.orchestrator import \
                SubAgentResult
            return SubAgentResult(states=[], elapsed_s=0.0, ok_count=0, error_count=0)

    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.SubAgentOrchestrator",
        _NoopOrch,
    )
    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda agent, *, session_id: object(),
    )

    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "cool-down-test", "_agent": object()},
    )
    r1 = await tool.execute(ctx)
    assert r1.is_success  # orquestrador stub não falha
    r2 = await tool.execute(ctx)
    assert r2.is_error
    assert r2.metadata.get("error_code") == "DISPATCH_COOLDOWN"


async def test_tool_validation_failure_does_not_consume_cooldown(monkeypatch):
    """Rejeições pré-cooldown (validação) NÃO devem consumir o slot."""
    tool = DispatchParallelSubagentsTool()

    class _NoopOrch:
        def __init__(self, *a, **kw):
            pass

        async def run(self, tasks):
            from deile.orchestration.subagents.orchestrator import \
                SubAgentResult
            return SubAgentResult(states=[], elapsed_s=0.0, ok_count=0, error_count=0)

    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.SubAgentOrchestrator",
        _NoopOrch,
    )
    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda agent, *, session_id: object(),
    )

    sess = "validation-doesnt-consume"
    # 1: payload inválido → BAD_REQUEST sem consumir cooldown
    bad_ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1)]},  # < 2 subtasks
        session_data={"session_id": sess, "_agent": object()},
    )
    bad = await tool.execute(bad_ctx)
    assert bad.is_error and bad.metadata.get("error_code") == "BAD_REQUEST"

    # 2: payload válido imediato → deve PASSAR (cooldown não foi consumido).
    ok_ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": sess, "_agent": object()},
    )
    ok = await tool.execute(ok_ctx)
    assert ok.is_success


async def test_tool_happy_path_calls_orchestrator(monkeypatch):
    """Patcheia o orquestrador e o resolver de runner: garante que a tool
    chama ``orchestrator.run`` com as tasks corretas e devolve o resumo.
    """
    tool = DispatchParallelSubagentsTool()

    # Stub do runner — não é exercitado nesse caminho (orchestrator é stub).
    class _StubRunner:
        async def run_one(self, state, *, on_event):
            state.status = "ok"
            state.started_at = 0.0
            state.finished_at = 0.05

    captured_tasks = {}

    class _StubOrchestrator:
        def __init__(self, runner, *, max_parallel, renderer_factory=None):
            captured_tasks["max_parallel"] = max_parallel
            self._runner = runner

        async def run(self, tasks):
            captured_tasks["tasks"] = tasks
            # Simula resultado.
            from deile.orchestration.subagents.orchestrator import \
                SubAgentResult
            states = []
            for t in tasks:
                st = SubAgentState(task=t)
                st.status = "ok"
                st.started_at = 0.0
                st.finished_at = 0.5
                st.result_text = f"done #{t.index}"
                st.add_file(f"file_{t.index}.py")
                states.append(st)
            return SubAgentResult(
                states=states, elapsed_s=0.5,
                ok_count=len(states), error_count=0,
            )

    # Patcheia onde a tool importou.
    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.SubAgentOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda agent, *, session_id: _StubRunner(),
    )

    ctx = ToolContext(
        user_input="",
        parsed_args={
            "subtasks": [
                _valid_subtask(1),
                _valid_subtask(2),
                _valid_subtask(3),
            ]
        },
        session_data={"session_id": "happy", "_agent": object()},
    )
    result = await tool.execute(ctx)

    assert result.is_success
    assert result.data["ok_count"] == 3
    assert result.data["error_count"] == 0
    assert result.data["ok_global"] is True
    assert len(result.data["subtasks"]) == 3
    # max_parallel respeita settings (default 3) mas é limitado pelo nº de tasks
    assert captured_tasks["max_parallel"] == 3
    # Tasks recebem index 1..N.
    assert [t.index for t in captured_tasks["tasks"]] == [1, 2, 3]


async def test_tool_schema_is_well_formed():
    """Verifica que o schema declara os campos esperados pra LLM."""
    tool = DispatchParallelSubagentsTool()
    schema = tool.schema
    assert schema.name == "dispatch_parallel_subagents"
    props = schema.parameters["properties"]
    assert "subtasks" in props
    items = props["subtasks"]["items"]["properties"]
    assert "description" in items
    assert "prompt" in items
    assert "persona" in items
    assert "model" in items
    assert "developer" in items["persona"]["enum"]
    assert schema.parameters["properties"]["subtasks"]["minItems"] == 2
    assert schema.parameters["properties"]["subtasks"]["maxItems"] == 5
