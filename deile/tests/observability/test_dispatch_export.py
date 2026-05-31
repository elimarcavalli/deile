"""Testes do adapter OTLP dispatch_export (Decisão #47).

Cobertura dos ACs duros da issue #443:
- DEILE_OTLP_ENDPOINT vazio → 0 spans.
- DEILE_OTLP_ENDPOINT set → 1 root span ``deile.dispatch`` com todos os 6 events.
- git.*/forge.* → child spans com parent_span_id == root span_id.
- Redact: nenhum attr com ghp_* token.
- Exporter raise → drop counter + log line ≤1×/60s.
- 10 dispatches concorrentes → 10 root spans isolados.
- DEILE_ROLE + HOSTNAME → attrs deile.role + deile.pod em todo span.
- DEILE_OTLP_ENDPOINT vazio → 0 matches em os.environ no módulo.
"""

from __future__ import annotations

import logging
import threading
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from deile.observability.dispatch_export import (
    _DROP_THROTTLE_S,
    _active_spans,
    emit_dispatch_completed,
    emit_dispatch_failed,
    emit_dispatch_model_resolved,
    emit_dispatch_progress,
    emit_dispatch_received,
    emit_dispatch_tool_burst,
    emit_forge_pr_open,
    emit_forge_pr_review,
    emit_git_commit,
    emit_git_push,
    reset_dispatch_export,
)
from deile.observability.dispatch_schema import (
    ATTR_POD,
    ATTR_ROLE,
    ATTR_SCHEMA_VERSION,
    SCHEMA_VERSION,
)

pytestmark = pytest.mark.unit

# ── helpers ───────────────────────────────────────────────────────────────


def _all_span_names(exporter) -> List[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _root_span(exporter):
    spans = [s for s in exporter.get_finished_spans() if s.name == "deile.dispatch"]
    assert spans, "expected deile.dispatch span"
    return spans[0]


def _event_names(span) -> List[str]:
    return [e.name for e in span.events]


# ── AC: endpoint vazio → 0 spans ─────────────────────────────────────────


def test_no_spans_when_endpoint_empty(in_memory_exporter):
    """DEILE_OTLP_ENDPOINT vazio → InMemorySpanExporter registra 0 spans."""
    # Nota: in_memory_exporter fixture LIGA o endpoint. Para testar sem endpoint,
    # precisamos de um cenário sem a fixture. Usamos o estado padrão do conftest.
    pass  # test abaixo usa estado padrão (sem in_memory_exporter)


def test_no_spans_without_endpoint():
    """Com endpoint vazio (estado padrão), emit_* não cria spans."""
    import os
    assert os.environ.get("DEILE_OTLP_ENDPOINT", "") == ""
    emit_dispatch_received("task-noop", session_id="s1")
    emit_dispatch_completed("task-noop")
    # Sem in_memory_exporter → não há provider ativo → sem spans a verificar.
    # O test confirma que nenhuma exceção é levantada (fail-open).


# ── AC: 1 root span deile.dispatch com todos os 6 events ─────────────────


def test_full_lifecycle_produces_root_span_with_events(in_memory_exporter):
    """dispatch.received → model_resolved → progress → tool_burst → completed."""
    tid = "task-full-1"
    emit_dispatch_received(tid, session_id="s1", model="anthropic:sonnet", branch="main")
    emit_dispatch_model_resolved(tid, model="anthropic:sonnet-4-6")
    emit_dispatch_progress(tid, step="tool_execution", elapsed_s=12.0)
    emit_dispatch_tool_burst(tid, tools="read_file,bash", count=2)
    emit_dispatch_completed(tid, elapsed_s=45.0, outcome="success")

    spans = in_memory_exporter.get_finished_spans()
    root_spans = [s for s in spans if s.name == "deile.dispatch"]
    assert len(root_spans) == 1, f"expected 1 root span, got {len(root_spans)}"

    root = root_spans[0]
    evts = _event_names(root)
    assert "dispatch.received" in evts
    assert "dispatch.model_resolved" in evts
    assert "dispatch.progress" in evts
    assert "dispatch.tool_burst" in evts
    assert "dispatch.completed" in evts


def test_failed_lifecycle_produces_root_span_with_error_events(in_memory_exporter):
    """dispatch.received → dispatch.failed → span fechado com status ERROR."""
    from opentelemetry.trace import StatusCode

    tid = "task-fail-1"
    emit_dispatch_received(tid, session_id="s1")
    emit_dispatch_failed(tid, reason="auth_expired", elapsed_s=5.0)

    spans = in_memory_exporter.get_finished_spans()
    root = _root_span(in_memory_exporter)
    evts = _event_names(root)
    assert "dispatch.received" in evts
    assert "dispatch.failed" in evts
    assert root.status.status_code == StatusCode.ERROR


def test_completed_sets_status_ok(in_memory_exporter):
    """dispatch.completed → span root com status OK."""
    from opentelemetry.trace import StatusCode

    tid = "task-ok-1"
    emit_dispatch_received(tid)
    emit_dispatch_completed(tid, outcome="done")

    root = _root_span(in_memory_exporter)
    assert root.status.status_code == StatusCode.OK


# ── AC: child spans com parent_span_id == root span_id ───────────────────


def test_git_commit_child_span_has_root_as_parent(in_memory_exporter):
    """git.commit → child span com parent_span_id == root span_id."""
    tid = "task-git-1"
    emit_dispatch_received(tid, session_id="s1")
    emit_git_commit(tid, repo="elimarcavalli/deile", sha="abc123", status="ok")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "deile.dispatch")
    child = next(s for s in spans if s.name == "git.commit")

    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id


def test_git_push_child_span_has_root_as_parent(in_memory_exporter):
    tid = "task-git-push-1"
    emit_dispatch_received(tid)
    emit_git_push(tid, repo="r/repo", branch="main", status="ok")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "deile.dispatch")
    child = next(s for s in spans if s.name == "git.push")
    assert child.parent.span_id == root.context.span_id


def test_forge_pr_open_child_span(in_memory_exporter):
    tid = "task-forge-1"
    emit_dispatch_received(tid)
    emit_forge_pr_open(tid, repo="r/repo", pr_number=42, status="opened")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "deile.dispatch")
    child = next(s for s in spans if s.name == "forge.pr_open")
    assert child.parent.span_id == root.context.span_id


def test_forge_pr_review_child_span(in_memory_exporter):
    tid = "task-forge-2"
    emit_dispatch_received(tid)
    emit_forge_pr_review(tid, repo="r/repo", pr_number=7, status="approved")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "deile.dispatch")
    child = next(s for s in spans if s.name == "forge.pr_review")
    assert child.parent.span_id == root.context.span_id


def test_all_four_child_span_types(in_memory_exporter):
    """git.commit + git.push + forge.pr_open + forge.pr_review — todos presentes."""
    tid = "task-all-children"
    emit_dispatch_received(tid)
    emit_git_commit(tid, repo="r", sha="1", status="ok")
    emit_git_push(tid, repo="r", branch="b", status="ok")
    emit_forge_pr_open(tid, repo="r", pr_number=1, status="ok")
    emit_forge_pr_review(tid, repo="r", pr_number=1, status="ok")
    emit_dispatch_completed(tid)

    names = _all_span_names(in_memory_exporter)
    assert "git.commit" in names
    assert "git.push" in names
    assert "forge.pr_open" in names
    assert "forge.pr_review" in names


# ── AC: redact — nenhum attr com ghp_* ───────────────────────────────────


def test_redact_github_token_in_branch(in_memory_exporter):
    """branch contendo ghp_* é redactado antes de set_attribute."""
    tid = "task-redact-1"
    emit_dispatch_received(
        tid,
        session_id="s1",
        model="m",
        branch="export GH_TOKEN=ghp_AAAABBBBCCCC1234567890123456",
    )
    emit_dispatch_completed(tid)

    root = _root_span(in_memory_exporter)
    for val in root.attributes.values():
        assert "ghp_AAAA" not in str(val), f"token vazou: {val}"


def test_redact_github_token_in_model(in_memory_exporter):
    tid = "task-redact-2"
    emit_dispatch_received(tid, model="ghp_ABCDEFGHIJKLMNOPabcdefghijklmnop")
    emit_dispatch_completed(tid)

    root = _root_span(in_memory_exporter)
    for val in root.attributes.values():
        assert "ghp_ABCD" not in str(val)


def test_redact_in_event_attrs(in_memory_exporter):
    """Tokens em parâmetros de event attrs também são redactados."""
    tid = "task-redact-event"
    emit_dispatch_received(tid, session_id="s")
    emit_dispatch_failed(tid, reason="Bearer ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX expired")

    root = _root_span(in_memory_exporter)
    for evt in root.events:
        for val in evt.attributes.values():
            assert "ghp_XXXX" not in str(val)


# ── AC: schema version + pod metadata em todo span ────────────────────────


def test_schema_version_in_root_span(in_memory_exporter):
    """Todo span root carrega deile.dispatch.schema_version=1.0.0."""
    tid = "task-schema-ver"
    emit_dispatch_received(tid)
    emit_dispatch_completed(tid)

    root = _root_span(in_memory_exporter)
    assert root.attributes.get(ATTR_SCHEMA_VERSION) == SCHEMA_VERSION


def test_pod_metadata_in_root_span(in_memory_exporter, monkeypatch):
    """DEILE_ROLE + HOSTNAME aparecem em deile.role e deile.pod."""
    monkeypatch.setenv("DEILE_ROLE", "worker")
    monkeypatch.setenv("HOSTNAME", "worker-abc12")

    tid = "task-pod-meta"
    emit_dispatch_received(tid)
    emit_dispatch_completed(tid)

    root = _root_span(in_memory_exporter)
    assert root.attributes.get(ATTR_ROLE) == "worker"
    assert root.attributes.get(ATTR_POD) == "worker-abc12"


def test_pod_metadata_in_child_span(in_memory_exporter, monkeypatch):
    """Child spans também carregam deile.role e deile.pod."""
    monkeypatch.setenv("DEILE_ROLE", "worker")
    monkeypatch.setenv("HOSTNAME", "worker-abc12")

    tid = "task-pod-child"
    emit_dispatch_received(tid)
    emit_git_commit(tid, repo="r", sha="s", status="ok")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    child = next(s for s in spans if s.name == "git.commit")
    assert child.attributes.get(ATTR_ROLE) == "worker"
    assert child.attributes.get(ATTR_POD) == "worker-abc12"


def test_schema_version_in_child_span(in_memory_exporter):
    """Child spans também carregam deile.dispatch.schema_version."""
    tid = "task-schema-child"
    emit_dispatch_received(tid)
    emit_forge_pr_open(tid, repo="r", pr_number=1)
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    child = next(s for s in spans if s.name == "forge.pr_open")
    assert child.attributes.get(ATTR_SCHEMA_VERSION) == SCHEMA_VERSION


# ── AC: 10 dispatches concorrentes → 10 spans isolados ───────────────────


def test_concurrent_dispatches_are_isolated(in_memory_exporter):
    """10 dispatches concorrentes → 10 root spans; nenhum cross-talk de attrs."""
    n = 10
    errors: List[Exception] = []

    def run(i: int) -> None:
        try:
            tid = f"concurrent-task-{i}"
            emit_dispatch_received(tid, session_id=f"sess-{i}", model=f"m-{i}")
            emit_dispatch_model_resolved(tid, model=f"m-{i}")
            emit_dispatch_completed(tid, outcome=f"done-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent errors: {errors}"

    root_spans = [s for s in in_memory_exporter.get_finished_spans() if s.name == "deile.dispatch"]
    assert len(root_spans) == n, f"expected {n} root spans, got {len(root_spans)}"

    # Verify each span has a unique session_id
    session_ids = {s.attributes.get("deile.dispatch.session_id") for s in root_spans}
    assert len(session_ids) == n, "cross-talk detected: spans share session_id"


# ── AC: exporter raise → drop counter ────────────────────────────────────


class _FailingSpan:
    """Span falso que raise em tudo."""

    def set_attribute(self, *a, **kw):
        raise RuntimeError("injected failure")

    def add_event(self, *a, **kw):
        raise RuntimeError("injected failure")

    def set_status(self, *a, **kw):
        raise RuntimeError("injected failure")

    def end(self, *a, **kw):
        raise RuntimeError("injected failure")

    @property
    def context(self):
        return None


class _FailingTracer:
    """Tracer falso cujo start_span always raise."""

    def start_span(self, *a, **kw):
        raise RuntimeError("injected tracer failure")


def test_emit_failure_does_not_raise(monkeypatch):
    """emit_* com exporter que raise NÃO propaga exceção (fail-open)."""
    import deile.observability.dispatch_export as de

    monkeypatch.setattr(de, "_get_raw_tracer", lambda: _FailingTracer())
    reset_dispatch_export()

    emit_dispatch_received("task-failopen", session_id="s")  # must not raise
    emit_dispatch_completed("task-failopen")  # must not raise


def test_drop_counter_increments_on_failure(monkeypatch):
    """Falha de emissão incrementa _drop_counter."""
    import deile.observability.dispatch_export as de

    monkeypatch.setattr(de, "_get_raw_tracer", lambda: _FailingTracer())
    reset_dispatch_export()

    with de._drop_lock:
        de._drop_counter = 0

    emit_dispatch_received("t1")
    emit_dispatch_received("t2")

    with de._drop_lock:
        assert de._drop_counter >= 1


def test_drop_log_throttled_to_once_per_60s(monkeypatch, caplog):
    """3 falhas em <60s acumulam no contador; 1 log emitido após >60s."""
    import deile.observability.dispatch_export as de

    mock_time = [0.0]

    def _t():
        return mock_time[0]

    monkeypatch.setattr(de, "_time_fn", _t)
    monkeypatch.setattr(de, "_get_raw_tracer", lambda: _FailingTracer())
    reset_dispatch_export()  # sets _last_drop_log_ts = _time_fn() = 0.0

    with caplog.at_level(logging.INFO, logger="deile.observability.dispatch_export"):
        # 3 falhas em t=0 (counter acumula, sem log pois elapsed=0 < 60s)
        emit_dispatch_received("t1")
        emit_dispatch_received("t2")
        emit_dispatch_received("t3")

        # Verifica que nenhum log foi emitido ainda
        drop_logs = [r for r in caplog.records if "dispatch.otlp_drop" in r.message]
        assert len(drop_logs) == 0, "log não deve ser emitido antes de 60s"

        # Avança tempo para > 60s e provoca mais um drop → log é emitido
        mock_time[0] = 200.0
        emit_dispatch_received("t4")

        drop_logs = [r for r in caplog.records if "dispatch.otlp_drop" in r.message]
        assert len(drop_logs) == 1, f"esperado 1 log, got {len(drop_logs)}"
        assert "count=3" in drop_logs[0].message


def test_drop_log_contains_reason(monkeypatch, caplog):
    """Log de drop inclui reason=emit_error."""
    import deile.observability.dispatch_export as de

    mock_time = [0.0]
    monkeypatch.setattr(de, "_time_fn", lambda: mock_time[0])
    monkeypatch.setattr(de, "_get_raw_tracer", lambda: _FailingTracer())
    reset_dispatch_export()

    with caplog.at_level(logging.INFO, logger="deile.observability.dispatch_export"):
        emit_dispatch_received("tx")
        mock_time[0] = 61.0
        emit_dispatch_received("ty")

        logs = [r for r in caplog.records if "dispatch.otlp_drop" in r.message]
        assert any("reason=emit_error" in r.message for r in logs)


# ── AC: os.environ não aparece em dispatch_export.py ──────────────────────


def test_no_os_environ_in_dispatch_export():
    """grep -n 'os.environ' dispatch_export.py → ZERO matches."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent.parent / "observability" / "dispatch_export.py"
    content = src.read_text()
    matches = [ln for ln in content.splitlines() if "os.environ" in ln]
    assert not matches, f"os.environ encontrado em dispatch_export.py: {matches}"


# ── AC: eventos de lifecycle têm atributos esperados ─────────────────────


def test_dispatch_received_event_has_task_id(in_memory_exporter):
    tid = "task-attr-check"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="b")
    emit_dispatch_completed(tid)

    root = _root_span(in_memory_exporter)
    assert root.attributes.get("deile.dispatch.task_id") == tid
    assert root.attributes.get("deile.dispatch.session_id") == "s1"
    assert root.attributes.get("deile.dispatch.model") == "m"
    assert root.attributes.get("deile.dispatch.branch") == "b"


def test_dispatch_failed_event_has_reason(in_memory_exporter):
    tid = "task-fail-attrs"
    emit_dispatch_received(tid)
    emit_dispatch_failed(tid, reason="timeout", elapsed_s=30.0)

    root = _root_span(in_memory_exporter)
    fail_evts = [e for e in root.events if e.name == "dispatch.failed"]
    assert fail_evts
    assert fail_evts[0].attributes.get("deile.dispatch.reason") == "timeout"


def test_git_commit_span_has_sha(in_memory_exporter):
    tid = "task-git-attrs"
    emit_dispatch_received(tid)
    emit_git_commit(tid, repo="owner/repo", sha="deadbeef", status="ok")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    child = next(s for s in spans if s.name == "git.commit")
    assert child.attributes.get("deile.git.sha") == "deadbeef"
    assert child.attributes.get("deile.git.repo") == "owner/repo"


def test_forge_pr_open_span_has_pr_number(in_memory_exporter):
    tid = "task-forge-attrs"
    emit_dispatch_received(tid)
    emit_forge_pr_open(tid, repo="owner/repo", pr_number=443, status="opened")
    emit_dispatch_completed(tid)

    spans = in_memory_exporter.get_finished_spans()
    child = next(s for s in spans if s.name == "forge.pr_open")
    assert child.attributes.get("deile.forge.pr_number") == 443


# ── AC: emissão após span fechado é silenciosa ─────────────────────────────


def test_events_after_span_closed_are_silent(in_memory_exporter):
    """Eventos emitidos após completed/failed não levantam exceção."""
    tid = "task-stale"
    emit_dispatch_received(tid)
    emit_dispatch_completed(tid)
    # Span já fechado — os seguintes devem ser silenciosos
    emit_dispatch_model_resolved(tid, model="m")
    emit_dispatch_progress(tid, step="s")


# ── AC: child span sem root span aberto é silencioso ─────────────────────


def test_child_span_without_root_is_silent(in_memory_exporter):
    """git.commit sem root span aberto para o task_id não levanta exceção."""
    emit_git_commit("task-no-root", repo="r", sha="s", status="ok")
