"""Testes de correlação trace↔log — issue #454 AC: trace_id/span_id.

Verifica que o LogRecord emitido em paralelo ao span event carrega os mesmos
trace_id e span_id do span OTel associado (D3).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _get_log_trace_ids(log_exporter):
    """Retorna lista de (trace_id, span_id) dos LogRecords finalizados."""
    return [
        (lr.log_record.trace_id, lr.log_record.span_id)
        for lr in log_exporter.get_finished_logs()
    ]


class TestLogSpanCorrelation:
    def test_dispatch_received_log_correlates_with_span(
        self, in_memory_exporter, in_memory_log_exporter
    ):
        """LogRecord de dispatch.received tem trace_id == span.trace_id."""
        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_received)

        emit_dispatch_received("corr-task-1", session_id="s1")
        emit_dispatch_completed("corr-task-1", elapsed_s=1.0)

        spans = in_memory_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        assert len(root_spans) >= 1, "expected root span"

        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) >= 1, "expected at least one LogRecord"

        # Find dispatch.received log record
        received_logs = [
            lr for lr in logs if "dispatch.received" in str(lr.log_record.body)
        ]
        assert len(received_logs) >= 1, "expected dispatch.received log record"

        root_span = root_spans[0]
        span_ctx = root_span.get_span_context()
        log_record = received_logs[0].log_record

        assert log_record.trace_id == span_ctx.trace_id, (
            f"trace_id mismatch: log={log_record.trace_id:#x} "
            f"span={span_ctx.trace_id:#x}"
        )
        assert log_record.span_id == span_ctx.span_id, (
            f"span_id mismatch: log={log_record.span_id:#x} "
            f"span={span_ctx.span_id:#x}"
        )

    def test_all_dispatch_events_have_valid_trace_id(
        self, in_memory_exporter, in_memory_log_exporter
    ):
        """Todos os 6 dispatch events produzem LogRecords com trace_id != 0."""
        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_model_resolved,
            emit_dispatch_progress, emit_dispatch_received,
            emit_dispatch_tool_burst)

        emit_dispatch_received("corr-all", session_id="s1")
        emit_dispatch_model_resolved("corr-all", model="m1")
        emit_dispatch_progress("corr-all", step="tool", elapsed_s=1.0)
        emit_dispatch_tool_burst("corr-all", tools="read", count=3)
        emit_dispatch_completed("corr-all", elapsed_s=5.0)

        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) >= 5, f"expected >= 5 log records, got {len(logs)}"

        for lr in logs:
            assert lr.log_record.trace_id != 0, "trace_id should not be 0"

    @pytest.mark.parametrize(
        "child_kind, body_marker",
        [
            ("git", "git.commit"),
            ("forge", "forge.pr_open"),
        ],
    )
    def test_child_span_log_has_parent_trace_id(
        self, in_memory_exporter, in_memory_log_exporter, child_kind, body_marker
    ):
        """LogRecord de child span (git.commit/forge.pr_open) tem o mesmo trace_id do root span."""
        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_received,
            emit_forge_pr_open, emit_git_commit)

        task_id = f"corr-{child_kind}"
        emit_dispatch_received(task_id, session_id="s1")
        if child_kind == "git":
            emit_git_commit(task_id, repo="owner/repo", sha="abc123", status="ok")
        else:
            emit_forge_pr_open(task_id, repo="owner/repo", pr_number=42, status="ok")
        emit_dispatch_completed(task_id, elapsed_s=2.0)

        spans = in_memory_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        assert len(root_spans) >= 1

        root_trace_id = root_spans[0].get_span_context().trace_id

        logs = in_memory_log_exporter.get_finished_logs()
        child_logs = [
            lr for lr in logs if body_marker in str(lr.log_record.body)
        ]
        assert len(child_logs) >= 1, f"expected {body_marker} log record"

        for lr in child_logs:
            assert lr.log_record.trace_id == root_trace_id, (
                f"{body_marker} log should have same trace_id as root span"
            )

    def test_failed_dispatch_log_correlation(
        self, in_memory_exporter, in_memory_log_exporter
    ):
        """dispatch.failed log tem trace_id do root span."""
        from deile.observability.dispatch_export import (
            emit_dispatch_failed, emit_dispatch_received)

        emit_dispatch_received("corr-fail", session_id="s1")
        emit_dispatch_failed("corr-fail", reason="auth_expired", elapsed_s=1.0)

        spans = in_memory_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        assert len(root_spans) >= 1

        root_trace_id = root_spans[0].get_span_context().trace_id
        logs = in_memory_log_exporter.get_finished_logs()
        failed_logs = [
            lr for lr in logs if "dispatch.failed" in str(lr.log_record.body)
        ]
        assert len(failed_logs) >= 1

        for lr in failed_logs:
            assert lr.log_record.trace_id == root_trace_id
            # Also verify severity is ERROR for auth_expired
            assert lr.log_record.severity_text == "ERROR"
