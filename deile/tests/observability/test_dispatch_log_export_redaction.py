"""Testes de redação em LogRecords — issue #454 D5.

Verifica que nenhum token/segredo vaza via body ou attributes do LogRecord.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


SENSITIVE_PATTERNS = [
    ("ghp_token", "ghp_" + "A" * 40),
    ("glpat_token", "glpat-" + "A" * 25),
    ("sk_token", "sk-" + "A" * 30),
    ("bearer_token", "Bearer abcdefghij123456789012345"),
]


class TestRedactionInLogRecords:
    @pytest.mark.parametrize(
        "attrs,secrets",
        [
            pytest.param({"deile.dispatch.token": secret}, [secret], id=label)
            for label, secret in SENSITIVE_PATTERNS
        ]
        + [
            # Caso multi-chave: múltiplos segredos em attrs distintos → todos
            # redactados pelo mesmo loop de _safe_attrs/body_for.
            pytest.param(
                {"token_a": "ghp_" + "A" * 40, "token_b": "sk-" + "B" * 30},
                ["ghp_" + "A" * 40, "sk-" + "B" * 30],
                id="multiple_secrets",
            )
        ],
    )
    def test_secret_in_attr_is_redacted(self, in_memory_log_exporter, attrs, secrets):
        """Segredo(s) em attr string → nenhum vaza no LogRecord (body nem attrs)."""
        from deile.observability.dispatch_log_export import emit_log_record

        emit_log_record(
            event_name="dispatch.received",
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes=attrs,
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        record = logs[0].log_record

        body = str(record.body)
        for secret in secrets:
            assert secret not in body, f"secret leaked in body: {body!r}"
            # Check attributes
            for k, v in (record.attributes or {}).items():
                assert secret not in str(v), (
                    f"secret leaked in attribute {k!r}: {v!r}"
                )

    @pytest.mark.parametrize("label,secret", SENSITIVE_PATTERNS)
    def test_secret_in_body_is_redacted(self, in_memory_log_exporter, label, secret):
        """Segredo no body do LogRecord está redactado."""
        from deile.observability.dispatch_log_export import emit_log_record

        emit_log_record(
            event_name="dispatch.received",
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes={"deile.dispatch.data": secret},
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        body = str(logs[0].log_record.body)
        assert secret not in body, f"secret leaked in body: {body!r}"
        assert "[REDACTED]" in body

    def test_safe_values_not_altered(self, in_memory_log_exporter):
        """Valores sem segredos não são alterados."""
        from deile.observability.dispatch_log_export import emit_log_record

        attrs = {
            "deile.dispatch.task_id": "task-123",
            "deile.dispatch.model": "anthropic:sonnet",
            "deile.dispatch.branch": "main",
        }

        emit_log_record(
            event_name="dispatch.received",
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes=attrs,
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        record = logs[0].log_record
        # Values should pass through unchanged
        for k, v in attrs.items():
            if record.attributes and k in record.attributes:
                assert record.attributes[k] == v
