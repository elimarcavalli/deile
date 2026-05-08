"""Unit tests for ScheduleStore + Schedule + catch-up."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deile.orchestration.pipeline.scheduler import (OneshotEntry, PendingRun,
                                                    RecurringEntry, Schedule,
                                                    ScheduleError,
                                                    ScheduleStore)


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TestRecurringEntry:
    def test_valid_construction(self):
        e = RecurringEntry(id="r1", action="review", cron="*/5 * * * *")
        assert e.enabled
        assert e.replay_all is False

    def test_invalid_action(self):
        with pytest.raises(ScheduleError):
            RecurringEntry(id="r1", action="bad", cron="* * * * *")

    def test_invalid_cron(self):
        with pytest.raises(ScheduleError):
            RecurringEntry(id="r1", action="review", cron="not a cron")

    def test_invalid_id(self):
        with pytest.raises(ScheduleError):
            RecurringEntry(id="bad/id", action="review", cron="* * * * *")


class TestOneshotEntry:
    def test_valid_construction(self):
        e = OneshotEntry(id="o1", action="implement", run_at=_utc("2026-05-06T18:00:00"))
        assert not e.completed

    def test_naive_run_at_treated_as_utc(self):
        e = OneshotEntry(id="o1", action="implement",
                         run_at=datetime(2026, 5, 6, 18, 0, 0))
        assert e.run_at.tzinfo is not None

    def test_invalid_action(self):
        with pytest.raises(ScheduleError):
            OneshotEntry(id="o1", action="bad", run_at=_utc("2026-05-06T18:00:00"))


class TestSchedule:
    def test_add_recurring_rejects_duplicate_id(self):
        s = Schedule()
        s.add_recurring(RecurringEntry(id="r1", action="review", cron="* * * * *"))
        with pytest.raises(ScheduleError):
            s.add_recurring(RecurringEntry(id="r1", action="implement", cron="* * * * *"))

    def test_remove_returns_true_when_found(self):
        s = Schedule()
        s.add_recurring(RecurringEntry(id="r1", action="review", cron="* * * * *"))
        assert s.remove("r1")
        assert not s.remove("r1")  # already removed


class TestComputePending:
    def test_no_pending_when_empty(self):
        s = Schedule()
        assert s.compute_pending() == []

    def test_recurring_not_due_yet(self):
        s = Schedule()
        # `0 0 1 1 *` = once a year on Jan 1 00:00. With last_run_at set to
        # the most recent Jan 1, the next run is far in the future regardless
        # of when this test runs.
        now = datetime.now(timezone.utc)
        last_jan_1 = datetime(now.year if now.month > 1 or now.day > 1 else now.year - 1,
                              1, 1, 0, 0, tzinfo=timezone.utc)
        s.add_recurring(RecurringEntry(
            id="r1", action="review", cron="0 0 1 1 *",
            last_run_at=last_jan_1,
        ))
        assert s.compute_pending() == []

    def test_recurring_coalesced_to_single_run(self):
        # last_run_at = 1 hour ago. Cron */5 → 12 misses. Default coalesces to 1.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        s = Schedule()
        s.add_recurring(RecurringEntry(
            id="r1", action="review", cron="*/5 * * * *",
            last_run_at=long_ago,
        ))
        pending = s.compute_pending()
        assert len(pending) == 1
        assert pending[0].entry_id == "r1"
        assert pending[0].action == "review"

    def test_recurring_replay_all_emits_each(self):
        long_ago = datetime.now(timezone.utc) - timedelta(minutes=20)
        s = Schedule()
        s.add_recurring(RecurringEntry(
            id="r1", action="review", cron="*/5 * * * *",
            last_run_at=long_ago, replay_all=True,
        ))
        pending = s.compute_pending()
        # 4 missed slots in 20 minutes (5, 10, 15, 20)
        assert len(pending) >= 3

    def test_disabled_recurring_skipped(self):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        s = Schedule()
        s.add_recurring(RecurringEntry(
            id="r1", action="review", cron="*/5 * * * *",
            last_run_at=long_ago, enabled=False,
        ))
        assert s.compute_pending() == []

    def test_oneshot_due(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        s = Schedule()
        s.add_oneshot(OneshotEntry(id="o1", action="implement", run_at=past, target_issue=99))
        pending = s.compute_pending()
        assert len(pending) == 1
        assert pending[0].is_oneshot
        assert pending[0].target_issue == 99

    def test_oneshot_not_yet_due(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        s = Schedule()
        s.add_oneshot(OneshotEntry(id="o1", action="implement", run_at=future))
        assert s.compute_pending() == []

    def test_completed_oneshot_skipped(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        s = Schedule()
        s.add_oneshot(OneshotEntry(id="o1", action="implement", run_at=past, completed=True))
        assert s.compute_pending() == []

    def test_pending_sorted_chronologically(self):
        now = datetime.now(timezone.utc)
        s = Schedule()
        s.add_oneshot(OneshotEntry(id="late", action="review",
                                   run_at=now - timedelta(minutes=1)))
        s.add_oneshot(OneshotEntry(id="early", action="review",
                                   run_at=now - timedelta(minutes=10)))
        pending = s.compute_pending()
        assert [p.entry_id for p in pending] == ["early", "late"]


class TestMarkRun:
    def test_mark_recurring_advances_last_run(self):
        s = Schedule()
        s.add_recurring(RecurringEntry(id="r1", action="review", cron="*/5 * * * *"))
        when = datetime.now(timezone.utc)
        run = PendingRun(when=when, entry_id="r1", action="review", is_oneshot=False)
        s.mark_run(run, when=when)
        assert s.get_recurring("r1").last_run_at == when

    def test_mark_oneshot_completes(self):
        s = Schedule()
        s.add_oneshot(OneshotEntry(
            id="o1", action="implement",
            run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        run = PendingRun(when=datetime.now(timezone.utc), entry_id="o1",
                         action="implement", is_oneshot=True)
        s.mark_run(run)
        assert s.get_oneshot("o1").completed


class TestScheduleStore:
    def test_load_returns_empty_when_missing(self, tmp_path):
        store = ScheduleStore(tmp_path, monitor_id="default")
        s = store.load()
        assert s.recurring == [] and s.oneshot == []

    def test_save_and_reload_roundtrips(self, tmp_path):
        store = ScheduleStore(tmp_path, monitor_id="m-alfa")
        s = Schedule()
        s.add_recurring(RecurringEntry(id="r1", action="review", cron="*/5 * * * *"))
        s.add_oneshot(OneshotEntry(
            id="o1", action="implement",
            run_at=_utc("2026-05-06T18:00:00"), target_issue=99,
        ))
        store.save(s)
        assert store.path.exists()
        assert store.path.name == "pipeline_schedule_m-alfa.yaml"
        # Roundtrip
        s2 = store.load()
        assert len(s2.recurring) == 1
        assert s2.recurring[0].id == "r1"
        assert len(s2.oneshot) == 1
        assert s2.oneshot[0].target_issue == 99

    def test_save_creates_config_dir(self, tmp_path):
        store = ScheduleStore(tmp_path, monitor_id="default")
        assert not (tmp_path / "config").exists()
        store.save(Schedule())
        assert (tmp_path / "config").is_dir()
