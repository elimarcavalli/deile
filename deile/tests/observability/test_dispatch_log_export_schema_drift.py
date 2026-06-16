"""Testes de schema drift entre span events e log records — issue #454.

Verifica que as chaves de atributos nos LogRecords são as mesmas que
as chaves nos span events correspondentes (D3 — same schema).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestSchemaDrift:
    def test_dispatch_received_log_attrs_match_schema(self, in_memory_log_exporter):
        """LogRecord de dispatch.received tem os mesmos attr keys que o schema."""
        from deile.observability.dispatch_log_export import emit_log_record
        from deile.observability.dispatch_schema import DispatchReceivedAttrs

        schema = DispatchReceivedAttrs(
            task_id="t1", session_id="s1", model="m1", branch="b1"
        )
        event_attrs = schema.to_span_attrs()

        emit_log_record(
            event_name=DispatchReceivedAttrs.EVENT_NAME,
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes=event_attrs,
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        record = logs[0].log_record

        # Log record attributes should contain all schema keys
        record_keys = set((record.attributes or {}).keys())
        schema_keys = set(event_attrs.keys())
        assert schema_keys.issubset(
            record_keys
        ), f"schema keys missing from log record: {schema_keys - record_keys}"

    def test_dispatch_failed_log_attrs_match_schema(self, in_memory_log_exporter):
        """LogRecord de dispatch.failed tem os mesmos attr keys que o schema."""
        from deile.observability.dispatch_log_export import emit_log_record
        from deile.observability.dispatch_schema import DispatchFailedAttrs

        schema = DispatchFailedAttrs(reason="auth_expired", elapsed_s=1.0)
        event_attrs = schema.to_event_attrs()

        emit_log_record(
            event_name=DispatchFailedAttrs.EVENT_NAME,
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes=event_attrs,
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        record = logs[0].log_record
        record_keys = set((record.attributes or {}).keys())
        schema_keys = set(event_attrs.keys())
        assert schema_keys.issubset(record_keys)

    def test_git_commit_log_attrs_match_schema(self, in_memory_log_exporter):
        """LogRecord de git.commit tem os mesmos attr keys que o schema."""
        from deile.observability.dispatch_log_export import emit_log_record
        from deile.observability.dispatch_schema import GitCommitAttrs

        schema = GitCommitAttrs(repo="owner/repo", sha="abc123", status="ok")
        span_attrs = schema.to_span_attrs()

        emit_log_record(
            event_name=GitCommitAttrs.SPAN_NAME,
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes=span_attrs,
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        record = logs[0].log_record
        record_keys = set((record.attributes or {}).keys())
        schema_keys = set(span_attrs.keys())
        assert schema_keys.issubset(record_keys)

    def test_body_contains_event_name(self, in_memory_log_exporter):
        """body_for() inclui o event_name como primeiro token."""
        from deile.observability.dispatch_log_export import body_for

        body = body_for("dispatch.received", {"key": "value"})
        assert body.startswith(
            "dispatch.received"
        ), f"body should start with event_name: {body!r}"

    def test_body_is_deterministic(self, in_memory_log_exporter):
        """body_for() é determinístico (mesmos inputs → mesmo output)."""
        from deile.observability.dispatch_log_export import body_for

        attrs = {"c": "3", "a": "1", "b": "2"}
        body1 = body_for("event", attrs)
        body2 = body_for("event", attrs)
        assert body1 == body2

    def test_body_keys_sorted(self):
        """body_for() ordena as chaves."""
        from deile.observability.dispatch_log_export import body_for

        body = body_for("event", {"z": "last", "a": "first", "m": "mid"})
        parts = body.split(" ")
        keys = [p.split("=")[0] for p in parts[1:]]
        assert keys == sorted(keys)
