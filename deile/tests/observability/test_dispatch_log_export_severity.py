"""Testes exaustivos da severity matrix (D4) — issue #454.

Cada linha da matrix é coberta + valores de borda.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "event_name,attrs,expected_text,expected_number",
    [
        # Rule 1: dispatch.failed reason=auth_expired → ERROR/17
        ("dispatch.failed", {"deile.dispatch.reason": "auth_expired"}, "ERROR", 17),
        # Rule 2: dispatch.failed other reasons → WARN/13
        ("dispatch.failed", {"deile.dispatch.reason": "timeout"}, "WARN", 13),
        ("dispatch.failed", {"deile.dispatch.reason": "worker_error"}, "WARN", 13),
        ("dispatch.failed", {"deile.dispatch.reason": ""}, "WARN", 13),
        ("dispatch.failed", {}, "WARN", 13),
        # Rule 3: dispatch.tool_burst count>50 → WARN/13
        ("dispatch.tool_burst", {"deile.dispatch.tool_count": 51}, "WARN", 13),
        ("dispatch.tool_burst", {"deile.dispatch.tool_count": 100}, "WARN", 13),
        # Rule 4: dispatch.tool_burst count<=50 → INFO/9
        ("dispatch.tool_burst", {"deile.dispatch.tool_count": 50}, "INFO", 9),
        ("dispatch.tool_burst", {"deile.dispatch.tool_count": 1}, "INFO", 9),
        ("dispatch.tool_burst", {"deile.dispatch.tool_count": 0}, "INFO", 9),
        ("dispatch.tool_burst", {}, "INFO", 9),
        # Rule 5: git.*/forge.* status=fail/error → WARN/13
        ("git.commit", {"deile.git.status": "fail"}, "WARN", 13),
        ("git.commit", {"deile.git.status": "error"}, "WARN", 13),
        ("git.commit", {"deile.git.status": "failed"}, "WARN", 13),
        ("git.push", {"deile.git.status": "fail"}, "WARN", 13),
        ("forge.pr_open", {"deile.forge.status": "fail"}, "WARN", 13),
        ("forge.pr_review", {"deile.forge.status": "error"}, "WARN", 13),
        # Rule 5: git.*/forge.* success → INFO/9
        ("git.commit", {"deile.git.status": "ok"}, "INFO", 9),
        ("git.push", {"deile.git.status": "ok"}, "INFO", 9),
        ("forge.pr_open", {"deile.forge.status": "ok"}, "INFO", 9),
        # forge.pr_review decision=CHANGES_REQUESTED is still INFO (operationally ok)
        ("forge.pr_review", {"deile.forge.status": "ok"}, "INFO", 9),
        # Rule 6: all others → INFO/9
        ("dispatch.received", {}, "INFO", 9),
        ("dispatch.model_resolved", {}, "INFO", 9),
        ("dispatch.progress", {}, "INFO", 9),
        ("dispatch.completed", {}, "INFO", 9),
        ("unknown.event", {}, "INFO", 9),
    ],
)
def test_severity_matrix(event_name, attrs, expected_text, expected_number):
    from deile.observability.dispatch_log_export import _severity_for

    text, number = _severity_for(event_name, attrs)
    assert text == expected_text, (
        f"event={event_name!r} attrs={attrs}: expected {expected_text}, got {text}"
    )
    assert number == expected_number, (
        f"event={event_name!r} attrs={attrs}: expected {expected_number}, got {number}"
    )


class TestSeverityInEmittedRecords:
    """Verify severity ends up in emitted LogRecords."""

    @pytest.mark.parametrize(
        "event_name,attrs,expected_text,expected_number",
        [
            # ERROR/17: dispatch.failed reason=auth_expired
            (
                "dispatch.failed",
                {"deile.dispatch.reason": "auth_expired"},
                "ERROR",
                17,
            ),
            # WARN/13: dispatch.tool_burst count>50
            (
                "dispatch.tool_burst",
                {"deile.dispatch.tool_count": 51},
                "WARN",
                13,
            ),
            # INFO/9: dispatch.received (default rule)
            ("dispatch.received", {}, "INFO", 9),
        ],
    )
    def test_severity_in_record(
        self,
        in_memory_log_exporter,
        event_name,
        attrs,
        expected_text,
        expected_number,
    ):
        from deile.observability.dispatch_log_export import emit_log_record

        emit_log_record(
            event_name,
            trace_id=1,
            span_id=1,
            trace_flags=1,
            attributes=attrs,
        )
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 1
        assert logs[0].log_record.severity_text == expected_text
        assert logs[0].log_record.severity_number.value == expected_number
