"""Tests para ``WorkerImplementer`` integração com DispatchLedger e fluxo
de resume (issue #309 fase 3.5)."""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.implementer import (WorkerImplementer,
                                                       _outcome_from_worker_response)


def _fake_client(dispatch_response: Dict[str, Any],
                  resume_info_response: Optional[Dict[str, Any]] = None,
                  resume_info_exc: Optional[Exception] = None):
    """Fake client compatível com WorkerImplementer (dispatch + get_resume_info)."""
    client = AsyncMock()
    client.dispatch = AsyncMock(return_value=dispatch_response)
    if resume_info_exc is not None:
        client.get_resume_info = AsyncMock(side_effect=resume_info_exc)
    else:
        client.get_resume_info = AsyncMock(return_value=resume_info_response or {})
    return client


def test_outcome_extracts_task_and_session_id():
    """_outcome_from_worker_response extrai task_id + session_id do response."""
    response = {
        "ok": True, "summary": "ok",
        "task_id": "abc123def456789a", "session_id": "uuid-abc",
    }
    outcome = _outcome_from_worker_response(response)
    assert outcome.ok is True
    assert outcome.task_id == "abc123def456789a"
    assert outcome.session_id == "uuid-abc"


def test_outcome_extracts_task_id_on_failure():
    """task_id é capturado mesmo quando ok=False — necessário pro reaper
    saber qual dispatch foi tentado."""
    response = {
        "ok": False, "error": "some error",
        "task_id": "failedhex0123456", "session_id": "sess-x",
    }
    outcome = _outcome_from_worker_response(response)
    assert outcome.ok is False
    assert outcome.task_id == "failedhex0123456"
    assert outcome.session_id == "sess-x"


def test_outcome_handles_missing_fields():
    """Worker antigo sem task_id/session_id no response → strings vazias."""
    response = {"ok": True, "summary": "old worker"}
    outcome = _outcome_from_worker_response(response)
    assert outcome.task_id == ""
    assert outcome.session_id == ""


@pytest.mark.asyncio
async def test_dispatch_records_in_ledger_on_failure(tmp_path):
    """Dispatch que retorna ok=False → ledger grava task_id+session_id pra
    próxima tentativa poder fazer resume."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    client = _fake_client({
        "ok": False, "error": "timeout",
        "task_id": "task001", "session_id": "sess001",
    })
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    out = await impl._dispatch(
        "do work", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100",
    )
    assert out.ok is False
    record = ledger.get("pr:100")
    assert record is not None
    assert record["task_id"] == "task001"
    assert record["session_id"] == "sess001"
    assert record["stage"] == "pr_review"


@pytest.mark.asyncio
async def test_dispatch_clears_ledger_on_success(tmp_path):
    """Dispatch que retorna ok=True → ledger limpa entrada (work feito)."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    # Pré-popula uma entrada (de tentativa anterior).
    ledger.record("pr:100", task_id="old", session_id="old-sess")

    client = _fake_client({
        "ok": True, "summary": "merged",
        "task_id": "new", "session_id": "new-sess",
    })
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    out = await impl._dispatch(
        "review", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100",
    )
    assert out.ok is True
    assert ledger.get("pr:100") is None  # limpou


@pytest.mark.asyncio
async def test_resume_consults_resume_info_when_resume_true(tmp_path):
    """resume=True + ledger tem entry → consulta get_resume_info."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:100", task_id="prevTaskId123456", session_id="sess-X")

    client = _fake_client(
        dispatch_response={
            "ok": True, "summary": "done resumed",
            "task_id": "prevTaskId123456", "session_id": "sess-X",
        },
        resume_info_response={
            "task_id": "prevTaskId123456", "session_id": "sess-X",
            "workdir": "/tmp/wd", "workdir_exists": True,
            "claude_alive": False, "last_is_error": False,
        },
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    await impl._dispatch(
        "continue", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100", resume=True,
    )
    # get_resume_info foi consultado (1+ vezes — pode ser chamado também
    # pelo nudge builder; issue #347 follow-up surface info dentro do meta).
    client.get_resume_info.assert_awaited()
    # dispatch recebeu payload com resume_session_id + prev_task_id.
    dispatch_call = client.dispatch.await_args
    payload = dispatch_call.kwargs.get("payload") or dispatch_call.args[0]
    assert payload.get("resume_session_id") == "sess-X"
    assert payload.get("prev_task_id") == "prevTaskId123456"
    # Campos internos (_*) NUNCA vão pro wire.
    assert "_last_result_summary" not in payload
    assert "_last_completed_at" not in payload


@pytest.mark.asyncio
async def test_resume_skips_when_claude_still_alive(tmp_path):
    """resume=True + worker diz claude_alive=True → NÃO faz dispatch."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:100", task_id="prevTask123", session_id="sess-Y")

    client = _fake_client(
        dispatch_response={"ok": True, "task_id": "x", "session_id": "y"},
        resume_info_response={
            "task_id": "prevTask123", "session_id": "sess-Y",
            "workdir": "/tmp/wd", "workdir_exists": True,
            "claude_alive": True,  # ← ainda rodando
        },
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    out = await impl._dispatch(
        "continue", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100", resume=True,
    )
    # Outcome indica skip.
    assert out.ok is False
    assert "DISPATCH_SKIPPED_STILL_RUNNING" in out.error
    # dispatch NÃO foi chamado.
    client.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_fallback_fresh_when_workdir_lost(tmp_path):
    """resume=True + workdir_exists=False → fallback fresh dispatch (sem
    resume_session_id no payload) + limpa ledger entry stale."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:100", task_id="lostTask", session_id="sess-Z")

    client = _fake_client(
        dispatch_response={"ok": True, "task_id": "new", "session_id": "new-sess"},
        resume_info_response={
            "task_id": "lostTask", "session_id": "sess-Z",
            "workdir": "/gone", "workdir_exists": False,
            "claude_alive": False,
        },
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    await impl._dispatch(
        "continue", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100", resume=True,
    )
    # ledger entry stale foi limpa antes do dispatch (vai limpar de novo
    # após success — net effect: cleared).
    assert ledger.get("pr:100") is None
    # dispatch foi fresh (sem resume_session_id).
    payload = client.dispatch.await_args.kwargs.get("payload") or \
              client.dispatch.await_args.args[0]
    assert "resume_session_id" not in payload
    assert "prev_task_id" not in payload


@pytest.mark.asyncio
async def test_resume_fallback_fresh_when_resume_info_404(tmp_path):
    """resume-info 404 (NOT_FOUND): worker sem metadata → fallback fresh +
    limpa ledger."""
    from deile.infrastructure.deile_worker_client import WorkerDispatchError
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:100", task_id="missingTask", session_id="sess-Q")

    client = _fake_client(
        dispatch_response={"ok": True, "task_id": "new", "session_id": "sX"},
        resume_info_exc=WorkerDispatchError("not found", error_code="NOT_FOUND"),
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    out = await impl._dispatch(
        "continue", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100", resume=True,
    )
    assert out.ok is True
    # dispatch foi fresh.
    payload = client.dispatch.await_args.kwargs.get("payload") or \
              client.dispatch.await_args.args[0]
    assert "resume_session_id" not in payload


@pytest.mark.asyncio
async def test_resume_transparent_even_when_caller_passes_resume_false(tmp_path):
    """Issue #347 follow-up: resume é AUTO-DETECTADO via ledger, mesmo
    quando caller passa resume=False. Cobre o caso comum de re-review
    após operador remover ~workflow:bloqueada (PR volta pra pendente,
    pipeline trata como 'fresh' mas ledger tem entry preservada → resume).
    """
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:100", task_id="prevTask", session_id="sess-X")

    client = _fake_client(
        dispatch_response={"ok": True, "summary": "ok", "task_id": "prevTask", "session_id": "sess-X"},
        resume_info_response={
            "task_id": "prevTask", "session_id": "sess-X",
            "workdir": "/tmp/wd", "workdir_exists": True,
            "claude_alive": False, "last_completed_at": 1234567,
            "last_result_summary": "STATUS: REQUEST_CHANGES",
        },
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    await impl._dispatch(
        "fresh review brief", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100", resume=False,  # ← False, mas resume acontece
    )
    # get_resume_info FOI chamado (auto-detect).
    client.get_resume_info.assert_awaited()
    # Payload tem resume_session_id (resume real).
    payload = client.dispatch.await_args.kwargs.get("payload") or \
              client.dispatch.await_args.args[0]
    assert payload.get("resume_session_id") == "sess-X"
    assert payload.get("prev_task_id") == "prevTask"


@pytest.mark.asyncio
async def test_no_ledger_key_skips_ledger_ops(tmp_path):
    """Quando ledger_key=None (caller que não suporta resume), ledger não
    é tocado — backward compat com testes existentes."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    client = _fake_client({
        "ok": True, "summary": "ok", "task_id": "t", "session_id": "s",
    })
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://x",
                              ledger=ledger)
    await impl._dispatch(
        "x", channel_id="pipeline-pr-1",
        # ledger_key omitido (default None)
    )
    assert ledger.list_all() == {}


@pytest.mark.asyncio
async def test_resume_records_worker_kind_based_on_url(tmp_path):
    """worker_kind no ledger é derivado da URL — 'claude' se aponta pra
    claude-worker, senão 'deile'."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    client = _fake_client({
        "ok": False, "error": "x", "task_id": "t", "session_id": "s",
    })
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://claude-worker:8767",
                              ledger=ledger)
    await impl._dispatch(
        "x", channel_id="pipeline-pr-100",
        stage="pr_review", ledger_key="pr:100",
    )
    assert ledger.get("pr:100")["worker_kind"] == "claude"


# --------------------------------------------------------------------------- #
# Issue #347 follow-up: smart review resume — testes novos
# --------------------------------------------------------------------------- #


def test_review_was_blocked_detects_request_changes():
    from deile.orchestration.pipeline.implementer import _review_was_blocked
    assert _review_was_blocked("## Veredict\nSTATUS: REQUEST_CHANGES\n")
    assert _review_was_blocked("blah blah\nSTATUS: request_changes  # case-insensitive\n")
    assert _review_was_blocked("STATUS: BLOCKED_no_progress\n")
    assert _review_was_blocked("STATUS: BLOCKED_motivo_qualquer")


def test_review_was_blocked_returns_false_on_approve():
    from deile.orchestration.pipeline.implementer import _review_was_blocked
    assert not _review_was_blocked("## Veredict\nSTATUS: APPROVE\n")
    assert not _review_was_blocked("STATUS: SUCCESS\n")
    assert not _review_was_blocked("")
    assert not _review_was_blocked("vou avaliar se isso bloqueia algum teste")
    assert not _review_was_blocked(None)  # tolerante


def test_estimate_session_tokens_from_jsonl_sums_usage():
    from deile.orchestration.pipeline.implementer import _estimate_session_tokens_from_jsonl
    jsonl = """
{"type":"user","content":"hi"}
{"type":"assistant","message":{"usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":1000,"cache_creation_input_tokens":200}}}
{"type":"assistant","message":{"usage":{"input_tokens":80,"output_tokens":30}}}
"""
    assert _estimate_session_tokens_from_jsonl(jsonl) == 100 + 50 + 1000 + 200 + 80 + 30


def test_estimate_session_tokens_from_jsonl_tolerates_malformed():
    from deile.orchestration.pipeline.implementer import _estimate_session_tokens_from_jsonl
    jsonl = """
not json
{"type":"assistant"}
{"type":"assistant","message":{"usage":{"input_tokens":10}}}
"""
    assert _estimate_session_tokens_from_jsonl(jsonl) == 10


def test_estimate_session_tokens_from_jsonl_empty():
    from deile.orchestration.pipeline.implementer import _estimate_session_tokens_from_jsonl
    assert _estimate_session_tokens_from_jsonl("") == 0
    assert _estimate_session_tokens_from_jsonl(None) == 0


@pytest.mark.asyncio
async def test_dispatch_preserves_ledger_when_review_was_blocked(tmp_path):
    """Bug crítico do reviewer #1: ok=True com STATUS: REQUEST_CHANGES NÃO
    deve limpar o ledger. Próximo dispatch tem que poder resume."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    client = _fake_client({
        "ok": True,
        "summary": "Comment posted. STATUS: REQUEST_CHANGES\nBlocked because...",
        "task_id": "tBlocked", "session_id": "sBlocked",
    })
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://claude-worker:8767",
                              ledger=ledger)
    await impl._dispatch(
        "review brief", channel_id="pipeline-pr-352",
        stage="pr_review", ledger_key="pr:352", resume=False,
    )
    # Ledger PRESERVADO mesmo com ok=True (porque review_was_blocked).
    record = ledger.get("pr:352")
    assert record is not None
    assert record["task_id"] == "tBlocked"


@pytest.mark.asyncio
async def test_dispatch_clears_ledger_on_normal_approve(tmp_path):
    """ok=True SEM STATUS: REQUEST_CHANGES limpa ledger (caminho happy)."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:200", task_id="prev", session_id="prev-s")
    client = _fake_client({
        "ok": True,
        "summary": "PR mergeada. STATUS: APPROVE\nmergeCommit=abc123",
        "task_id": "tOk", "session_id": "sOk",
    })
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://claude-worker:8767",
                              ledger=ledger)
    await impl._dispatch(
        "review brief", channel_id="pipeline-pr-200",
        stage="pr_review", ledger_key="pr:200", resume=False,
    )
    assert ledger.get("pr:200") is None  # cleared


@pytest.mark.asyncio
async def test_resume_brief_for_pr_review_uses_rich_nudge(tmp_path):
    """Quando há resume_meta + stage='pr_review', _dispatch chama
    _wrap_review_brief_for_resume e o brief enviado é o nudge rico
    (não o brief original do caller)."""
    ledger = DispatchLedger(path=tmp_path / "l.json")
    ledger.record("pr:300", task_id="prevT", session_id="prevS")

    client = _fake_client(
        dispatch_response={"ok": True, "summary": "ok", "task_id": "prevT", "session_id": "prevS"},
        resume_info_response={
            "task_id": "prevT", "session_id": "prevS",
            "workdir": "/tmp/x", "workdir_exists": True,
            "claude_alive": False,
            "last_completed_at": 1779800000,
            "last_result_summary": "STATUS: REQUEST_CHANGES — Item 1 não resolvido; Item 2 OK.",
        },
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://claude-worker:8767",
                              ledger=ledger)
    await impl._dispatch(
        brief="ORIGINAL BRIEF SHOULD NOT BE USED",
        channel_id="pipeline-pr-300", stage="pr_review",
        ledger_key="pr:300", resume=False,
    )
    payload = client.dispatch.await_args.kwargs.get("payload") or \
              client.dispatch.await_args.args[0]
    brief_sent = payload.get("brief", "")
    # Nudge marker presente.
    assert "RESUME DE REVIEW" in brief_sent
    # Prev verdict incluído.
    assert "REQUEST_CHANGES" in brief_sent
    # Instruções anti-redundância.
    assert "NÃO releia" in brief_sent
    assert "NÃO rode a suite completa" in brief_sent
    # Brief original NÃO está lá.
    assert "ORIGINAL BRIEF SHOULD NOT BE USED" not in brief_sent


@pytest.mark.asyncio
async def test_resume_brief_only_for_pr_review_stage(tmp_path):
    """Implement stage NÃO usa nudge review — usa brief original do caller."""
    ledger = DispatchLedger(path=tmp_path / "issue.json")
    ledger.record("issue:50", task_id="implT", session_id="implS")

    client = _fake_client(
        dispatch_response={"ok": True, "task_id": "implT", "session_id": "implS"},
        resume_info_response={
            "task_id": "implT", "session_id": "implS",
            "workdir": "/tmp/y", "workdir_exists": True, "claude_alive": False,
        },
    )
    impl = WorkerImplementer(client=client,
                              endpoint_override="http://claude-worker:8767",
                              ledger=ledger)
    await impl._dispatch(
        brief="IMPLEMENT BRIEF",
        channel_id="pipeline-issue-50", stage="implement",
        ledger_key="issue:50", resume=False,
    )
    payload = client.dispatch.await_args.kwargs.get("payload") or \
              client.dispatch.await_args.args[0]
    # Brief original preservado.
    assert payload["brief"] == "IMPLEMENT BRIEF"
    assert "RESUME DE REVIEW" not in payload["brief"]
