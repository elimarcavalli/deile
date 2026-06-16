"""Regression test for SQLiteTaskManager._get_tasks_for_list cache timestamp bug.

Bug: after fetching tasks from DB and writing _task_cache[list_id], the method
never wrote _cache_timestamps[list_id]. Since _is_cache_valid returns False when
list_id is absent from _cache_timestamps, the freshly-populated task cache was
immediately invalid on the next call, causing a DB round-trip every time.

Fix: _get_tasks_for_list now writes _cache_timestamps[list_id] after _task_cache,
mirroring the pattern in load_task_list (lines 432-434).
"""

from __future__ import annotations

from unittest.mock import patch

import aiosqlite
import pytest

from deile.orchestration.sqlite_task_manager import SQLiteTaskManager


@pytest.fixture()
async def manager(tmp_path):
    m = SQLiteTaskManager(db_path=tmp_path / "tasks.db")
    await m._ensure_schema()
    return m


@pytest.mark.unit
async def test_get_tasks_for_list_populates_cache_timestamp(manager) -> None:
    """After _get_tasks_for_list, list_id must be in _cache_timestamps and cache must be valid."""
    list_id = "list-abc"

    await manager._get_tasks_for_list(list_id)

    assert (
        list_id in manager._cache_timestamps
    ), "_cache_timestamps must contain list_id after _get_tasks_for_list"
    assert manager._is_cache_valid(
        list_id
    ), "_is_cache_valid must return True immediately after _get_tasks_for_list"


@pytest.mark.unit
async def test_get_tasks_for_list_second_call_uses_cache(tmp_path) -> None:
    """Second call to _get_tasks_for_list must be served from cache without hitting the DB."""
    manager = SQLiteTaskManager(db_path=tmp_path / "tasks.db")
    await manager._ensure_schema()

    list_id = "list-xyz"

    # First call: hits DB and populates cache
    first_result = await manager._get_tasks_for_list(list_id)

    # Wrap aiosqlite.connect with a spy; second call must not trigger it
    connect_calls: list = []
    original_connect = aiosqlite.connect

    def spy_connect(*args, **kwargs):
        connect_calls.append(args)
        return original_connect(*args, **kwargs)

    with patch(
        "deile.orchestration.sqlite_task_manager.aiosqlite.connect", new=spy_connect
    ):
        second_result = await manager._get_tasks_for_list(list_id)

    assert connect_calls == [], (
        f"Cache miss on second call: DB was accessed {len(connect_calls)} time(s). "
        "The cache timestamp was not written by the first call."
    )
    assert second_result == first_result
