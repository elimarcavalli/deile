"""AC3 parity tests: verify CronRunner emits audit events with the correct field
contract (issue #508 follow-up of #437).

These tests do NOT mock the AuditLogger internals — they use a real (tmp_path)
AuditLogger to verify that the fields CronRunner passes to log_cron_fire /
log_cron_skipped round-trip correctly through the AuditEvent.details dict.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from deile.cron.runner import CronRunner, _payload_hash
from deile.cron.store import CronEntry, CronStore
from deile.security.audit_logger import AuditEventType, AuditLogger


@pytest.fixture
def store(tmp_path):
    return CronStore(tmp_path / "cron.db")


@pytest.fixture
def audit_logger(tmp_path):
    return AuditLogger(log_dir=str(tmp_path / "logs"))


def _due_entry(entry_id: str = "job-1", prompt: str = "run backup", cron: str = "* * * * *") -> CronEntry:
    # Use cron-only (no run_at) with last_fired_at far enough in the past that
    # next_fire_at ends up before now, making the entry immediately due.
    return CronEntry(
        id=entry_id,
        prompt=prompt,
        cron=cron,
        last_fired_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )


class TestCronFireFieldParity:
    """CronRunner._fire must emit CRON_FIRE with entry_id encoded in resource,
    plus name, schedule, payload_hash in details."""

    async def test_entry_id_in_resource(self, store, audit_logger) -> None:
        store.add(_due_entry("my-job"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        assert events, "expected CRON_FIRE event"
        assert events[0].resource == "cron:my-job"

    async def test_name_field_equals_entry_id(self, store, audit_logger) -> None:
        store.add(_due_entry("daily-renew"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        assert events[0].details["name"] == "daily-renew"

    async def test_schedule_field(self, store, audit_logger) -> None:
        store.add(_due_entry("sched-job", cron="* * * * *"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        assert events[0].details["schedule"] == "* * * * *"

    async def test_payload_hash_field_format(self, store, audit_logger) -> None:
        entry = _due_entry("hash-job", prompt="do something")
        store.add(entry)
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        ph = events[0].details["payload_hash"]
        assert isinstance(ph, str)
        assert ph.startswith("sha256:")
        assert ph == _payload_hash("do something")

    async def test_all_fire_fields_present(self, store, audit_logger) -> None:
        store.add(_due_entry("full-job", prompt="full prompt", cron="* * * * *"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_FIRE)
        assert events, "no CRON_FIRE event emitted"
        ev = events[0]
        assert ev.resource == "cron:full-job"
        for field in ("name", "schedule", "payload_hash"):
            assert field in ev.details, f"missing field {field!r} in CRON_FIRE details"


class TestCronSkippedFieldParity:
    """CronRunner._fire must emit CRON_SKIPPED with entry_id in resource,
    plus name and reason in details."""

    async def test_entry_id_in_resource(self, store, audit_logger) -> None:
        store.add(_due_entry("skip-job"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store)  # no callback
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_SKIPPED)
        assert events, "expected CRON_SKIPPED event"
        assert events[0].resource == "cron:skip-job"

    async def test_name_field(self, store, audit_logger) -> None:
        store.add(_due_entry("named-skip"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store)
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_SKIPPED)
        assert events[0].details["name"] == "named-skip"

    async def test_reason_field_no_callback(self, store, audit_logger) -> None:
        store.add(_due_entry("reason-job"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store)
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_SKIPPED)
        assert events[0].details["reason"] == "no callback"

    async def test_all_skipped_fields_present(self, store, audit_logger) -> None:
        store.add(_due_entry("allfields-skip"))
        with patch("deile.cron.runner.get_audit_logger", return_value=audit_logger):
            runner = CronRunner(store)
            await runner.tick()
        events = audit_logger.get_recent_events(event_type=AuditEventType.CRON_SKIPPED)
        assert events, "no CRON_SKIPPED event emitted"
        ev = events[0]
        assert ev.resource == "cron:allfields-skip"
        for field in ("name", "reason"):
            assert field in ev.details, f"missing field {field!r} in CRON_SKIPPED details"
