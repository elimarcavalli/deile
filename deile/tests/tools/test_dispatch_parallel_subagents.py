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

import pytest

from deile.orchestration.subagents.events import SubAgentState, SubAgentTask
from deile.tools.base import ToolContext
from deile.tools.dispatch_parallel_subagents import (
    DispatchParallelSubagentsTool,
    _build_tasks_from_payload,
)

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
            from deile.orchestration.subagents.orchestrator import SubAgentResult

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
            from deile.orchestration.subagents.orchestrator import SubAgentResult

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
        def __init__(
            self, runner, *, max_parallel, renderer_factory=None, capture_output=True
        ):
            captured_tasks["max_parallel"] = max_parallel
            captured_tasks["capture_output"] = capture_output
            self._runner = runner

        async def run(self, tasks):
            captured_tasks["tasks"] = tasks
            # Simula resultado.
            from deile.orchestration.subagents.orchestrator import SubAgentResult

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
                states=states,
                elapsed_s=0.5,
                ok_count=len(states),
                error_count=0,
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


async def test_tool_persists_summary_to_session_history(monkeypatch):
    """Fix #2: ao terminar, a tool grava entrada role=assistant com
    metadata HISTORY_MARKER_KEY na conversation_history — sobrevive /resume.
    """
    tool = DispatchParallelSubagentsTool()
    from deile.tools.dispatch_parallel_subagents import HISTORY_MARKER_KEY

    class _NoopOrch:
        def __init__(self, *a, **kw):
            pass

        async def run(self, tasks):
            from deile.orchestration.subagents.orchestrator import SubAgentResult

            states = []
            for t in tasks:
                st = SubAgentState(task=t)
                st.status = "ok"
                st.started_at = 0.0
                st.finished_at = 0.5
                st.add_file(f"file_{t.index}.py")
                states.append(st)
            return SubAgentResult(
                states=states,
                elapsed_s=0.5,
                ok_count=len(states),
                error_count=0,
            )

    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.SubAgentOrchestrator", _NoopOrch
    )
    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda agent, *, session_id: object(),
    )

    # Mock agente com session que tem add_to_history
    class _FakeSession:
        def __init__(self):
            self.history = []

        def add_to_history(self, role, content, metadata=None):
            self.history.append(
                {"role": role, "content": content, "metadata": metadata or {}}
            )

    fake_session = _FakeSession()
    fake_agent = type("FakeAgent", (), {"_sessions": {"hist-test": fake_session}})()

    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "hist-test", "_agent": fake_agent},
    )
    result = await tool.execute(ctx)
    assert result.is_success

    # Histórico recebeu UMA entrada de panel summary
    panel_entries = [
        h
        for h in fake_session.history
        if h["role"] == "assistant" and h["metadata"].get(HISTORY_MARKER_KEY)
    ]
    assert len(panel_entries) == 1
    e = panel_entries[0]
    assert "Sub-DEILEs paralelos" in e["content"]
    assert e["metadata"]["ok_count"] == 2
    assert e["metadata"]["error_count"] == 0
    assert e["metadata"]["n_subtasks"] == 2


async def test_tool_persistence_failure_does_not_break_tool(monkeypatch):
    """Persistência é best-effort: agente sem _sessions, ou sem add_to_history,
    ou exception arbitrária, NÃO derruba a tool — só loga em debug.
    """
    tool = DispatchParallelSubagentsTool()

    class _NoopOrch:
        def __init__(self, *a, **kw):
            pass

        async def run(self, tasks):
            from deile.orchestration.subagents.orchestrator import SubAgentResult

            return SubAgentResult(states=[], elapsed_s=0.1, ok_count=0, error_count=0)

    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.SubAgentOrchestrator", _NoopOrch
    )
    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda agent, *, session_id: object(),
    )

    # Agent sem _sessions
    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "x", "_agent": object()},
    )
    result = await tool.execute(ctx)
    assert result.is_success  # mesmo sem persistência conseguida


async def test_recursion_guard_blocks_nested_calls(monkeypatch):
    """Fix complementar: dispatch_parallel_subagents recursivo é fora de escopo.
    O ContextVar _NESTING_DEPTH bloqueia chamada aninhada.
    """
    from deile.tools.dispatch_parallel_subagents import _NESTING_DEPTH

    tool = DispatchParallelSubagentsTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "nest", "_agent": object()},
    )
    # Simula que já estamos dentro de uma chamada
    token = _NESTING_DEPTH.set(1)
    try:
        result = await tool.execute(ctx)
    finally:
        _NESTING_DEPTH.reset(token)
    assert result.is_error
    assert result.metadata.get("error_code") == "RECURSION_DENIED"


async def test_tool_cleans_subagent_sessions_after_run(monkeypatch):
    """Fix B1: cada sub-DEILE cria session_id ``subagent_*`` no agente; sem
    cleanup, ``agent._sessions`` cresce indefinidamente. O LocalRunner deve
    deletar a sub-session no finally (ou erro).
    """
    from deile.orchestration.subagents.runner import LocalSubAgentRunner

    class _FakeAgent:
        def __init__(self):
            self._sessions = {}
            self._call_count = 0

        def process_input_stream(self, prompt, **kwargs):
            sid = kwargs["session_id"]
            self._sessions[sid] = object()  # simula registro
            self._call_count += 1

            async def _gen():
                return
                yield  # pragma: no cover

            return _gen()

    agent = _FakeAgent()
    runner = LocalSubAgentRunner(agent)
    state = SubAgentState(
        task=SubAgentTask(
            index=1,
            description="x",
            prompt="p" * 60,
        )
    )
    await runner.run_one(state, on_event=lambda _: None)

    assert agent._call_count == 1
    # Sessão foi criada E deletada — não fica residual.
    assert agent._sessions == {}


async def test_safe_truncate_markdown_handles_unclosed_code_fences():
    """Fix H3: truncar em 400 chars não pode deixar code-fence aberta."""
    from deile.tools.dispatch_parallel_subagents import _safe_truncate_markdown

    bad = "Algumas linhas\n\n```python\n" + "x" * 600
    result = _safe_truncate_markdown(bad, max_chars=200)
    # Deve fechar a fence com ``` no final
    assert result.count("```") % 2 == 0, f"odd fences: {result!r}"


async def test_safe_truncate_markdown_prefers_paragraph_break():
    """Truncamento preferencial em \\n\\n na janela [70%..100%] do limite."""
    from deile.tools.dispatch_parallel_subagents import _safe_truncate_markdown

    text = "A" * 250 + "\n\n" + "B" * 300
    result = _safe_truncate_markdown(text, max_chars=400)
    # Cortou no \n\n (índice 250), não em 400.
    assert "B" not in result
    assert len(result) <= 400


async def test_safe_truncate_markdown_short_text_passthrough():
    """Texto curto retorna inalterado (sem ellipsis)."""
    from deile.tools.dispatch_parallel_subagents import _safe_truncate_markdown

    short = "tudo certo aqui"
    assert _safe_truncate_markdown(short, max_chars=400) == short
    assert _safe_truncate_markdown("", max_chars=400) == ""


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
    # Iter-2 review: defense-in-depth — additionalProperties:false rejeita
    # campos extras antes do spread para SubAgentTask.__init__.
    assert (
        schema.parameters["properties"]["subtasks"]["items"]["additionalProperties"]
        is False
    )


async def test_session_lock_lru_does_not_evict_the_just_touched_session(monkeypatch):
    """MN1 (iter-3): a eviction LRU NUNCA pode remover o ``session_id`` que
    acabou de ser inserido/tocado.

    Edge case: se TODAS as entradas anteriores estiverem ``locked()``, o loop
    chegaria à recém-inserida e a removeria (ela tem o ``id`` no ``ordered_ids``
    e está livre). Outro caller concorrente para o MESMO session_id, ao buscar
    o lock, encontraria ``None`` e criaria um lock NOVO — quebrando o mutex
    per-sessão. O guard ``if sid == session_id: continue`` impede esse caminho.
    """
    # Cap pequeno + TODAS as entradas anteriores travadas pra forçar
    # o cenário onde a única entrada livre é a recém-tocada.
    monkeypatch.setattr(DispatchParallelSubagentsTool, "_SESSION_LOCKS_MAX", 2)

    # 3 sessions PRÉ-existentes — todas LOCKED.
    locked_sids = ["old_a", "old_b", "old_c"]
    locked_locks = []
    for sid in locked_sids:
        lk = asyncio.Lock()
        await lk.acquire()
        DispatchParallelSubagentsTool._SESSION_LOCKS[sid] = lk
        locked_locks.append(lk)
    try:
        # Pede um novo lock — vai inserir "fresh" (4 total) e disparar eviction
        # com excess=2. Sem o guard MN1, a varredura pularia old_a/old_b/old_c
        # (locked), chegaria em "fresh" (livre) e removeria — depois um caller
        # concorrente para "fresh" criaria um lock NOVO.
        fresh_lock = await DispatchParallelSubagentsTool._get_session_lock("fresh")
        assert fresh_lock is not None

        # Pede o MESMO session_id novamente. Deve retornar o MESMO lock
        # (mutex per-sessão preservado).
        same_lock = await DispatchParallelSubagentsTool._get_session_lock("fresh")
        assert same_lock is fresh_lock, (
            "MN1 quebrado: a entrada recém-tocada foi evicted, e a próxima "
            "chamada criou um lock NOVO — outro caller concorrente para o "
            "mesmo session_id teria seu próprio mutex (race condition)."
        )

        # "fresh" continua presente; o cap foi violado (overshoot transitório
        # esperado quando todas as outras estão locked — comportamento MA1).
        assert "fresh" in DispatchParallelSubagentsTool._SESSION_LOCKS
    finally:
        for lk in locked_locks:
            lk.release()


async def test_session_lock_lru_eviction_skips_locked_without_break(monkeypatch):
    """MA1 (iter-2): a eviction LRU não pode parar no primeiro lock-em-uso —
    deve PULAR (continue) e seguir evictando os não-locked subsequentes.
    Antes, ``break`` na primeira entrada locked parava a poda permanentemente.
    """
    # Cap pequeno pra forçar eviction.
    monkeypatch.setattr(DispatchParallelSubagentsTool, "_SESSION_LOCKS_MAX", 3)

    # Cria 5 entradas: o lock #0 (oldest) FICA travado; os outros 4 são livres.
    # Quando _get_session_lock("new") inserir a 6ª entrada, eviction precisa
    # rodar e remover 3 (excess=3); deve PULAR o #0 locked e remover #1, #2, #3.
    locks = []
    for i in range(5):
        lk = asyncio.Lock()
        DispatchParallelSubagentsTool._SESSION_LOCKS[f"sess_{i}"] = lk
        locks.append(lk)
    # Trava o oldest.
    await locks[0].acquire()
    try:
        # Pede um novo lock — vai inserir "new" (6 total) e disparar eviction.
        new_lock = await DispatchParallelSubagentsTool._get_session_lock("new")
        assert new_lock is not None
        # Estado pós-eviction: cap=3, removidos 3 não-locked, locked #0 ficou.
        # OrderedDict atual deve conter {sess_0 (locked), sess_4, new}.
        remaining = list(DispatchParallelSubagentsTool._SESSION_LOCKS.keys())
        assert "sess_0" in remaining, "locked entry MUST NOT be evicted"
        assert "new" in remaining
        # E o tamanho voltou ao cap.
        assert len(DispatchParallelSubagentsTool._SESSION_LOCKS) == 3
    finally:
        locks[0].release()


async def test_audit_terminal_event_is_cancelled_when_orchestrator_raises_cancellederror(
    monkeypatch,
):
    """MN3 (iter-3): quando ``orchestrator.run()`` propaga CancelledError
    (parent cancel), o evento terminal de auditoria deve ter ``result='cancelled'``
    em vez do default ``'failure'``.

    Antes, o handler do ``execute()`` capturava só ``Exception`` no try
    externo; CancelledError pulava o bloco que mapeia ``result.cancelled``
    em ``audit_result``, e o ``finally`` emitia com o default 'failure' —
    perda de fidelidade do desfecho real.
    """
    captured: list[dict] = []

    class _FakeAuditLogger:
        def log_event(self, **kwargs):
            captured.append(kwargs)

    fake_logger = _FakeAuditLogger()
    import deile.security.audit_logger as audit_mod

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: fake_logger)

    async def _cancel_run(self, tasks):
        # Simula parent-cancel propagando do orchestrator.
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "deile.orchestration.subagents.SubAgentOrchestrator.run",
        _cancel_run,
    )

    class _StubRunner:
        pass

    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda *a, **kw: _StubRunner(),
    )

    tool = DispatchParallelSubagentsTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "mn3-cancel-audit", "_agent": object()},
    )

    # A tool deve propagar CancelledError (Pilar 03 §6).
    with pytest.raises(asyncio.CancelledError):
        await tool.execute(ctx)

    # Mesmo com a exceção, o ``finally`` deve ter emitido o terminal
    # com result='cancelled' (não 'failure').
    tool_events = [
        e for e in captured if e.get("tool_name") == "dispatch_parallel_subagents"
    ]
    results = [e.get("result") for e in tool_events]
    assert "accepted" in results, "admission audit ausente"
    assert (
        "cancelled" in results
    ), f"MN3 quebrado: esperava terminal 'cancelled', got {results}"
    assert (
        "failure" not in results
    ), "MN3 regressão: terminal não pode ser 'failure' quando o caller cancela"


async def test_audit_emits_terminal_event(monkeypatch):
    """MA6 (iter-2): audit emite tanto na admissão (result='accepted')
    quanto no terminal (success/failure/cancelled/budget_exceeded) e usa
    self.name como tool_name (Pilar 4).
    """
    captured: list[dict] = []

    class _FakeAuditLogger:
        def log_event(self, **kwargs):
            captured.append(kwargs)

    fake_logger = _FakeAuditLogger()
    import deile.security.audit_logger as audit_mod

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: fake_logger)

    # Stub o orchestrator.run pra retornar success rápido.
    class _StubResult:
        ok_global = True
        cancelled = False
        ok_count = 2
        error_count = 0
        elapsed_s = 0.01
        states = []

        def consolidated_summary(self):
            return "ok"

        def markdown_summary(self):
            return "ok"

    async def _stub_run(self, tasks):
        return _StubResult()

    monkeypatch.setattr(
        "deile.orchestration.subagents.SubAgentOrchestrator.run",
        _stub_run,
    )

    # Stub resolve_runner para evitar criar runners reais.
    class _StubRunner:
        pass

    monkeypatch.setattr(
        "deile.tools.dispatch_parallel_subagents.resolve_runner",
        lambda *a, **kw: _StubRunner(),
    )

    tool = DispatchParallelSubagentsTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"subtasks": [_valid_subtask(1), _valid_subtask(2)]},
        session_data={"session_id": "audit-test", "_agent": object()},
    )
    result = await tool.execute(ctx)
    assert result.is_success or not result.is_error

    # Devem haver pelo menos 2 audit events para a tool: accepted + terminal.
    tool_events = [
        e for e in captured if e.get("tool_name") == "dispatch_parallel_subagents"
    ]
    assert len(tool_events) >= 2, f"expected accepted+terminal, got {tool_events}"
    results = [e.get("result") for e in tool_events]
    assert "accepted" in results
    assert "success" in results
    # NT4 (iter-3): actor é o papel ('tool'), tool_name é a identidade
    # específica via self.name — não-redundantes (antes eram iguais).
    for e in tool_events:
        assert e.get("actor") == "tool"
        assert e.get("tool_name") == "dispatch_parallel_subagents"
