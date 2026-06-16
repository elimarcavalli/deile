"""Unit tests for CronRunner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from deile.cron.runner import CronRunner
from deile.cron.store import CronEntry, CronStore


@pytest.fixture
def store(tmp_path):
    return CronStore(tmp_path / "cron.db")


class TestTick:
    async def test_tick_no_due_entries(self, store):
        runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
        fired = await runner.tick()
        assert fired == 0

    async def test_tick_fires_due_oneshot(self, store):
        store.add(
            CronEntry(
                id="o1",
                prompt="hi",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        cb = AsyncMock(return_value="result")
        runner = CronRunner(store, fire_callback=cb)
        fired = await runner.tick()
        assert fired == 1
        cb.assert_awaited_once()
        loaded = store.get("o1")
        assert not loaded.enabled
        assert loaded.last_result == "result"

    async def test_tick_no_callback_skips_gracefully(self, store):
        store.add(
            CronEntry(
                id="o1",
                prompt="hi",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        runner = CronRunner(store)  # no callback
        fired = await runner.tick()
        assert fired == 1  # counted as fired (to advance)
        loaded = store.get("o1")
        assert "no callback" in (loaded.last_result or "")

    async def test_tick_callback_exception_marks_error(self, store):
        store.add(
            CronEntry(
                id="o1",
                prompt="hi",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )

        async def boom(_e):
            raise RuntimeError("boom")

        runner = CronRunner(store, fire_callback=boom)
        await runner.tick()
        loaded = store.get("o1")
        assert "error" in (loaded.last_result or "").lower()
        # one-shot still advanced (disabled) so we don't re-fire forever
        assert not loaded.enabled

    async def test_tick_dms_result_when_configured(self, store):
        store.add(
            CronEntry(
                id="o1",
                prompt="hi",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                notify_user_id="42",
            )
        )

        dm_calls = []

        async def fake_dm(uid, text):
            dm_calls.append((uid, text))
            return {"ok": True}

        runner = CronRunner(
            store,
            fire_callback=AsyncMock(return_value="result-text"),
            notify_dm=fake_dm,
        )
        await runner.tick()
        assert len(dm_calls) == 1
        assert dm_calls[0][0] == "42"
        assert "result-text" in dm_calls[0][1]

    async def test_tick_emits_cron_fire_event(self, store):
        from unittest.mock import MagicMock, patch

        store.add(
            CronEntry(
                id="o1",
                prompt="hi",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        mock_logger = MagicMock()
        with patch("deile.cron.runner.get_audit_logger", return_value=mock_logger):
            runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
            await runner.tick()
        mock_logger.log_cron_fire.assert_called_once()
        call_kwargs = mock_logger.log_cron_fire.call_args
        assert call_kwargs[1]["entry_id"] == "o1" or call_kwargs[0][0] == "o1"

    async def test_tick_no_callback_emits_cron_skipped(self, store):
        from unittest.mock import MagicMock, patch

        store.add(
            CronEntry(
                id="o1",
                prompt="hi",
                run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        mock_logger = MagicMock()
        with patch("deile.cron.runner.get_audit_logger", return_value=mock_logger):
            runner = CronRunner(store)
            await runner.tick()
        mock_logger.log_cron_skipped.assert_called_once()


class TestLifecycle:
    async def test_start_then_stop(self, store):
        runner = CronRunner(
            store,
            fire_callback=AsyncMock(return_value="ok"),
            poll_interval_seconds=1,
        )
        await runner.start()
        assert runner.is_running
        await runner.stop()
        assert not runner.is_running

    async def test_start_idempotent(self, store):
        runner = CronRunner(store, fire_callback=AsyncMock(return_value="ok"))
        await runner.start()
        first_task = runner._task
        await runner.start()
        assert runner._task is first_task
        await runner.stop()
