"""Teste de drift — schema vs emit OTLP (Decisão #47 / D3).

Verifica que os atributos definidos nos dataclasses de ``dispatch_schema``
correspondem exatamente aos atributos emitidos por ``dispatch_export``.

Falha se:
  - schema define chave que o emit NÃO coloca no span/event;
  - SCHEMA_VERSION diverge do valor em ATTR_SCHEMA_VERSION.

O "emit textual" (dispatch_logger.py, issue #435) não existe em main ainda;
esta passada cobre o eixo OTLP. Drift vs wire textual entra quando #435 mergear.
"""

from __future__ import annotations

import pytest

from deile.observability.dispatch_export import (emit_dispatch_completed,
                                                 emit_dispatch_failed,
                                                 emit_dispatch_model_resolved,
                                                 emit_dispatch_progress,
                                                 emit_dispatch_received,
                                                 emit_dispatch_tool_burst,
                                                 emit_forge_pr_open,
                                                 emit_forge_pr_review,
                                                 emit_git_commit,
                                                 emit_git_push)
from deile.observability.dispatch_schema import (ATTR_SCHEMA_VERSION,
                                                 SCHEMA_VERSION,
                                                 DispatchCompletedAttrs,
                                                 DispatchFailedAttrs,
                                                 DispatchModelResolvedAttrs,
                                                 DispatchProgressAttrs,
                                                 DispatchReceivedAttrs,
                                                 DispatchToolBurstAttrs,
                                                 ForgePrOpenAttrs,
                                                 ForgePrReviewAttrs,
                                                 GitCommitAttrs, GitPushAttrs)

pytestmark = pytest.mark.unit

# ── helpers ────────────────────────────────────────────────────────────────


def _event_attrs(span, event_name: str) -> dict:
    for e in span.events:
        if e.name == event_name:
            return dict(e.attributes or {})
    return {}


def _find_span(exporter, name: str):
    for s in exporter.get_finished_spans():
        if s.name == name:
            return s
    return None


# ── SCHEMA_VERSION ─────────────────────────────────────────────────────────


def test_schema_version_constant_matches_attr(in_memory_exporter):
    """SCHEMA_VERSION e ATTR_SCHEMA_VERSION são consistentes no span emitido."""
    emit_dispatch_received("drift-sv-1", session_id="s")
    emit_dispatch_completed("drift-sv-1")

    span = _find_span(in_memory_exporter, "deile.dispatch")
    assert span is not None
    assert span.attributes.get(ATTR_SCHEMA_VERSION) == SCHEMA_VERSION


# ── dispatch.received ──────────────────────────────────────────────────────


def test_dispatch_received_schema_keys_present_in_span(in_memory_exporter):
    """Todas as chaves de DispatchReceivedAttrs estão presentes no root span."""
    tid = "drift-recv"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="b")
    emit_dispatch_completed(tid)

    span = _find_span(in_memory_exporter, DispatchReceivedAttrs.SPAN_NAME)
    assert span is not None
    span_keys = set(span.attributes.keys())
    for key in DispatchReceivedAttrs.expected_keys():
        assert key in span_keys, f"schema key '{key}' ausente no span"


# ── dispatch.model_resolved ────────────────────────────────────────────────


def test_dispatch_model_resolved_schema_keys_present_in_event(in_memory_exporter):
    """Chaves de DispatchModelResolvedAttrs estão no evento dispatch.model_resolved."""
    tid = "drift-mr"
    emit_dispatch_received(tid)
    emit_dispatch_model_resolved(tid, model="anthropic:sonnet")
    emit_dispatch_completed(tid)

    span = _find_span(in_memory_exporter, "deile.dispatch")
    attrs = _event_attrs(span, DispatchModelResolvedAttrs.EVENT_NAME)
    for key in DispatchModelResolvedAttrs.expected_keys():
        assert key in attrs, f"schema key '{key}' ausente no event"


# ── dispatch.progress ─────────────────────────────────────────────────────


def test_dispatch_progress_schema_keys_present_in_event(in_memory_exporter):
    tid = "drift-prog"
    emit_dispatch_received(tid)
    emit_dispatch_progress(tid, step="tool_execution", elapsed_s=10.0)
    emit_dispatch_completed(tid)

    span = _find_span(in_memory_exporter, "deile.dispatch")
    attrs = _event_attrs(span, DispatchProgressAttrs.EVENT_NAME)
    for key in DispatchProgressAttrs.expected_keys():
        assert key in attrs, f"schema key '{key}' ausente no event"


# ── dispatch.tool_burst ────────────────────────────────────────────────────


def test_dispatch_tool_burst_schema_keys_present_in_event(in_memory_exporter):
    tid = "drift-tb"
    emit_dispatch_received(tid)
    emit_dispatch_tool_burst(tid, tools="read_file,bash", count=2)
    emit_dispatch_completed(tid)

    span = _find_span(in_memory_exporter, "deile.dispatch")
    attrs = _event_attrs(span, DispatchToolBurstAttrs.EVENT_NAME)
    for key in DispatchToolBurstAttrs.expected_keys():
        assert key in attrs, f"schema key '{key}' ausente no event"


# ── dispatch.completed ────────────────────────────────────────────────────


def test_dispatch_completed_schema_keys_present_in_event(in_memory_exporter):
    tid = "drift-comp"
    emit_dispatch_received(tid)
    emit_dispatch_completed(tid, elapsed_s=60.0, outcome="success")

    span = _find_span(in_memory_exporter, "deile.dispatch")
    attrs = _event_attrs(span, DispatchCompletedAttrs.EVENT_NAME)
    for key in DispatchCompletedAttrs.expected_keys():
        assert key in attrs, f"schema key '{key}' ausente no event"


# ── dispatch.failed ───────────────────────────────────────────────────────


def test_dispatch_failed_schema_keys_present_in_event(in_memory_exporter):
    tid = "drift-fail"
    emit_dispatch_received(tid)
    emit_dispatch_failed(tid, reason="timeout", elapsed_s=5.0)

    span = _find_span(in_memory_exporter, "deile.dispatch")
    attrs = _event_attrs(span, DispatchFailedAttrs.EVENT_NAME)
    for key in DispatchFailedAttrs.expected_keys():
        assert key in attrs, f"schema key '{key}' ausente no event"


# ── git.commit ─────────────────────────────────────────────────────────────


def test_git_commit_schema_keys_present_in_span(in_memory_exporter):
    tid = "drift-gc"
    emit_dispatch_received(tid)
    emit_git_commit(tid, repo="r", sha="s", status="ok")
    emit_dispatch_completed(tid)

    child = _find_span(in_memory_exporter, GitCommitAttrs.SPAN_NAME)
    assert child is not None
    span_keys = set(child.attributes.keys())
    for key in GitCommitAttrs.expected_keys():
        assert key in span_keys, f"schema key '{key}' ausente no child span git.commit"


# ── git.push ──────────────────────────────────────────────────────────────


def test_git_push_schema_keys_present_in_span(in_memory_exporter):
    tid = "drift-gp"
    emit_dispatch_received(tid)
    emit_git_push(tid, repo="r", branch="main", status="ok")
    emit_dispatch_completed(tid)

    child = _find_span(in_memory_exporter, GitPushAttrs.SPAN_NAME)
    assert child is not None
    span_keys = set(child.attributes.keys())
    for key in GitPushAttrs.expected_keys():
        assert key in span_keys, f"schema key '{key}' ausente no child span git.push"


# ── forge.pr_open ─────────────────────────────────────────────────────────


def test_forge_pr_open_schema_keys_present_in_span(in_memory_exporter):
    tid = "drift-fpo"
    emit_dispatch_received(tid)
    emit_forge_pr_open(tid, repo="r", pr_number=1, status="ok")
    emit_dispatch_completed(tid)

    child = _find_span(in_memory_exporter, ForgePrOpenAttrs.SPAN_NAME)
    assert child is not None
    span_keys = set(child.attributes.keys())
    for key in ForgePrOpenAttrs.expected_keys():
        assert key in span_keys, f"schema key '{key}' ausente no child span forge.pr_open"


# ── forge.pr_review ───────────────────────────────────────────────────────


def test_forge_pr_review_schema_keys_present_in_span(in_memory_exporter):
    tid = "drift-fpr"
    emit_dispatch_received(tid)
    emit_forge_pr_review(tid, repo="r", pr_number=2, status="ok")
    emit_dispatch_completed(tid)

    child = _find_span(in_memory_exporter, ForgePrReviewAttrs.SPAN_NAME)
    assert child is not None
    span_keys = set(child.attributes.keys())
    for key in ForgePrReviewAttrs.expected_keys():
        assert key in span_keys, f"schema key '{key}' ausente no child span forge.pr_review"
