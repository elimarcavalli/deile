"""Tests for CRON_FIRE and CRON_SKIPPED audit event types (issue #437)."""
from __future__ import annotations

from pathlib import Path
import pytest
from deile.security.audit_logger import AuditEventType, AuditLogger, SeverityLevel


@pytest.fixture
def audit_logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(log_dir=str(tmp_path / "logs"))


class TestCronFireEvent:
    def test_log_cron_fire_emits_event(self, audit_logger: AuditLogger) -> None:
        initial = audit_logger.event_count()
        audit_logger.log_cron_fire(
            entry_id="job-1",
            name="daily-renew",
            schedule="0 3 * * *",
            payload_hash="sha256:abc123",
        )
        assert audit_logger.event_count() == initial + 1

    def test_log_cron_fire_event_type(self, audit_logger: AuditLogger) -> None:
        audit_logger.log_cron_fire("job-1", "daily-renew", "0 3 * * *", "sha256:abc")
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        assert events, "expected at least one CRON_FIRE event"
        ev = events[0]
        assert ev.event_type == AuditEventType.CRON_FIRE
        assert ev.severity == SeverityLevel.INFO
        assert ev.details["name"] == "daily-renew"
        assert ev.details["schedule"] == "0 3 * * *"
        assert ev.details["payload_hash"] == "sha256:abc"

    def test_log_cron_fire_none_fields(self, audit_logger: AuditLogger) -> None:
        audit_logger.log_cron_fire("job-2", None, None, None)
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        assert events
        assert events[0].details["name"] is None
        assert events[0].details["schedule"] is None


class TestCronSkippedEvent:
    def test_log_cron_skipped_emits_event(self, audit_logger: AuditLogger) -> None:
        initial = audit_logger.event_count()
        audit_logger.log_cron_skipped(entry_id="job-1", name="daily-renew", reason="no callback")
        assert audit_logger.event_count() == initial + 1

    def test_log_cron_skipped_event_type(self, audit_logger: AuditLogger) -> None:
        audit_logger.log_cron_skipped("job-1", "daily-renew", "no callback")
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_SKIPPED)
        assert events, "expected at least one CRON_SKIPPED event"
        ev = events[0]
        assert ev.event_type == AuditEventType.CRON_SKIPPED
        assert ev.severity == SeverityLevel.WARNING
        assert ev.details["reason"] == "no callback"

    def test_log_cron_skipped_disabled_reason(self, audit_logger: AuditLogger) -> None:
        audit_logger.log_cron_skipped("job-2", None, "disabled")
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_SKIPPED)
        assert events[0].details["reason"] == "disabled"


class TestAuditEventTypeEnum:
    def test_cron_fire_value(self) -> None:
        assert AuditEventType.CRON_FIRE.value == "cron_fire"

    def test_cron_skipped_value(self) -> None:
        assert AuditEventType.CRON_SKIPPED.value == "cron_skipped"
