"""Unit tests for CronEntry + CronStore (SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deile.cron.store import CronEntry, CronStore, CronStoreError, make_id


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TestCronEntryConstruction:
    def test_recurring_entry(self):
        e = CronEntry(id="x", prompt="hello", cron="*/5 * * * *")
        assert e.cron == "*/5 * * * *"
        assert e.run_at is None
        assert e.next_fire_at is not None
        assert not e.is_oneshot

    def test_oneshot_entry(self):
        e = CronEntry(id="x", prompt="ping", run_at=_utc("2030-01-01T00:00:00"))
        assert e.is_oneshot
        assert e.next_fire_at == _utc("2030-01-01T00:00:00")

    def test_naive_run_at_promoted_to_utc(self):
        e = CronEntry(id="x", prompt="ping", run_at=datetime(2030, 1, 1, 0, 0))
        assert e.run_at.tzinfo is not None

    def test_rejects_both_cron_and_run_at(self):
        with pytest.raises(CronStoreError):
            CronEntry(
                id="x", prompt="p", cron="* * * * *", run_at=_utc("2030-01-01T00:00:00")
            )

    def test_rejects_neither(self):
        with pytest.raises(CronStoreError):
            CronEntry(id="x", prompt="p")

    def test_rejects_empty_prompt(self):
        with pytest.raises(CronStoreError):
            CronEntry(id="x", prompt="   ", cron="* * * * *")

    def test_rejects_invalid_cron(self):
        with pytest.raises(CronStoreError):
            CronEntry(id="x", prompt="p", cron="not a cron")


class TestAdvance:
    def test_oneshot_disables_after_advance(self):
        e = CronEntry(id="x", prompt="p", run_at=_utc("2030-01-01T00:00:00"))
        e.advance()
        assert not e.enabled
        assert e.next_fire_at is None

    def test_recurring_advances(self):
        e = CronEntry(
            id="x",
            prompt="p",
            cron="*/5 * * * *",
            last_fired_at=_utc("2026-05-06T00:00:00"),
        )
        first = e.next_fire_at
        e.advance(after=_utc("2026-05-06T00:10:00"))
        assert e.next_fire_at > first


class TestCronStore:
    def test_add_and_get(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        e = CronEntry(id="r1", prompt="hello", cron="*/5 * * * *")
        store.add(e)
        loaded = store.get("r1")
        assert loaded is not None
        assert loaded.prompt == "hello"
        assert loaded.cron == "*/5 * * * *"

    def test_add_rejects_duplicate(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(id="r1", prompt="p", cron="*/5 * * * *"))
        with pytest.raises(CronStoreError):
            store.add(CronEntry(id="r1", prompt="p2", cron="*/10 * * * *"))

    def test_get_missing_returns_none(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        assert store.get("nope") is None

    def test_remove(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(id="r1", prompt="p", cron="*/5 * * * *"))
        assert store.remove("r1")
        assert not store.remove("r1")  # already gone
        assert store.get("r1") is None

    def test_set_enabled(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(id="r1", prompt="p", cron="*/5 * * * *"))
        assert store.set_enabled("r1", False)
        loaded = store.get("r1")
        assert not loaded.enabled

    def test_list_only_enabled(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(id="r1", prompt="a", cron="*/5 * * * *"))
        store.add(CronEntry(id="r2", prompt="b", cron="*/10 * * * *"))
        store.set_enabled("r2", False)
        all_e = store.list_all(only_enabled=False)
        only_on = store.list_all(only_enabled=True)
        assert len(all_e) == 2
        assert len(only_on) == 1
        assert only_on[0].id == "r1"

    def test_list_due(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        # one-shot in the past → due
        store.add(
            CronEntry(
                id="o1",
                prompt="p",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        # one-shot in the future → not due
        store.add(
            CronEntry(
                id="o2",
                prompt="p",
                run_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        due = store.list_due()
        assert [e.id for e in due] == ["o1"]

    def test_mark_fired_oneshot_disables(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(
            CronEntry(
                id="o1",
                prompt="p",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        store.mark_fired("o1", result="ok")
        loaded = store.get("o1")
        assert not loaded.enabled
        assert loaded.next_fire_at is None
        assert loaded.last_result == "ok"

    def test_mark_fired_recurring_advances(self, tmp_path):
        store = CronStore(tmp_path / "cron.db")
        store.add(CronEntry(id="r1", prompt="p", cron="*/5 * * * *"))
        before = store.get("r1").next_fire_at
        store.mark_fired("r1", when=datetime.now(timezone.utc) + timedelta(hours=1))
        after = store.get("r1").next_fire_at
        assert after > before


class TestMakeId:
    def test_unique(self):
        ids = {make_id() for _ in range(100)}
        assert len(ids) == 100

    def test_prefix(self):
        assert make_id().startswith("cron-")
