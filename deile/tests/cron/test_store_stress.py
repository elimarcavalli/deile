"""Stress / performance tests for CronStore.

Validates latency under bulk reads and thread-safety under concurrent writes.
These tests use threading (not asyncio) because CronStore is synchronous/
thread-safe by design.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from deile.cron.store import CronEntry, CronStore, make_id

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _utc_past(minutes: int = 5) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _utc_future(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _make_recurring(store_id: str, n: int) -> CronEntry:
    return CronEntry(id=store_id, prompt=f"prompt {n}", cron="*/5 * * * *")


def _make_oneshot_due(store_id: str, n: int) -> CronEntry:
    return CronEntry(id=store_id, prompt=f"oneshot {n}", run_at=_utc_past(minutes=10))


def _make_oneshot_future(store_id: str, n: int) -> CronEntry:
    return CronEntry(id=store_id, prompt=f"future {n}", run_at=_utc_future())


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.perf
class TestStorePerformance:

    def test_list_due_with_100_entries_is_fast(self, tmp_path):
        """list_due() with 100 entries completes in under 100 ms."""
        db = CronStore(tmp_path / "stress.db")

        # 50 due entries (past) + 50 future entries
        for i in range(50):
            db.add(_make_oneshot_due(make_id(), i))
        for i in range(50):
            db.add(_make_oneshot_future(make_id(), i))

        start = time.perf_counter()
        due = db.list_due()
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(due) == 50
        assert elapsed_ms < 100, f"list_due() took {elapsed_ms:.1f}ms (limit 100ms)"

    def test_concurrent_writes_no_corruption(self, tmp_path):
        """10 threads each adding 10 entries produces exactly 100 rows, no duplicates."""
        db = CronStore(tmp_path / "concurrent.db")
        errors: List[Exception] = []

        def add_entries():
            try:
                for _ in range(10):
                    db.add(CronEntry(id=make_id(), prompt="concurrent", cron="*/1 * * * *"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=add_entries) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised exceptions: {errors}"
        all_entries = db.list_all()
        assert len(all_entries) == 100, f"Expected 100 entries, got {len(all_entries)}"
        # All IDs must be unique.
        ids = [e.id for e in all_entries]
        assert len(ids) == len(set(ids)), "Duplicate IDs detected"

    def test_concurrent_read_during_writes(self, tmp_path):
        """list_all() does not raise or return corrupt data while writes occur."""
        db = CronStore(tmp_path / "read_write.db")
        stop_event = threading.Event()
        read_errors: List[Exception] = []
        write_errors: List[Exception] = []

        def writer():
            while not stop_event.is_set():
                try:
                    db.add(CronEntry(id=make_id(), prompt="write", cron="*/1 * * * *"))
                    time.sleep(0.005)
                except Exception as exc:  # noqa: BLE001
                    write_errors.append(exc)

        def reader():
            while not stop_event.is_set():
                try:
                    entries = db.list_all()
                    # Basic sanity: every returned entry must have a non-empty id
                    for e in entries:
                        assert e.id, "Entry with empty id returned from list_all"
                    time.sleep(0.003)
                except Exception as exc:  # noqa: BLE001
                    read_errors.append(exc)

        writer_thread = threading.Thread(target=writer, daemon=True)
        reader_thread = threading.Thread(target=reader, daemon=True)
        writer_thread.start()
        reader_thread.start()

        time.sleep(0.5)
        stop_event.set()
        writer_thread.join(timeout=2)
        reader_thread.join(timeout=2)

        assert not read_errors, f"Reader raised: {read_errors}"
        assert not write_errors, f"Writer raised: {write_errors}"

    def test_list_due_returns_only_enabled_entries(self, tmp_path):
        """Disabled entries are excluded from list_due()."""
        db = CronStore(tmp_path / "enabled.db")
        entry_id = make_id()
        db.add(_make_oneshot_due(entry_id, 0))

        # Disable it immediately.
        db.set_enabled(entry_id, False)

        due = db.list_due()
        assert all(e.id != entry_id for e in due), "Disabled entry appeared in list_due"

    def test_mark_fired_advances_recurring_and_disables_oneshot(self, tmp_path):
        """mark_fired() moves next_fire_at forward (recurring) or disables (one-shot)."""
        db = CronStore(tmp_path / "fired.db")

        rec_id = make_id()
        db.add(CronEntry(id=rec_id, prompt="recur", cron="*/5 * * * *"))
        old_next = db.get(rec_id).next_fire_at
        # Advance anchor well past next cron boundary so next_fire_at actually moves.
        fired_at = old_next + timedelta(minutes=6)
        db.mark_fired(rec_id, when=fired_at)
        new_next = db.get(rec_id).next_fire_at
        assert new_next > old_next, "Recurring next_fire_at should advance after firing"

        os_id = make_id()
        db.add(CronEntry(id=os_id, prompt="oneshot", run_at=_utc_past(minutes=1)))
        db.mark_fired(os_id)
        entry = db.get(os_id)
        assert not entry.enabled, "One-shot should be disabled after mark_fired"

    def test_list_all_returns_all_including_disabled(self, tmp_path):
        """list_all() without only_enabled returns disabled entries too."""
        db = CronStore(tmp_path / "all.db")
        ids = [make_id() for _ in range(5)]
        for e_id in ids:
            db.add(CronEntry(id=e_id, prompt="p", cron="*/5 * * * *"))

        # Disable 2 of them
        db.set_enabled(ids[0], False)
        db.set_enabled(ids[1], False)

        all_entries = db.list_all(only_enabled=False)
        assert len(all_entries) == 5

        enabled_only = db.list_all(only_enabled=True)
        assert len(enabled_only) == 3
