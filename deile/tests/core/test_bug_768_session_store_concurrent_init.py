"""xfail test for bug #768: SessionStore.init() missing double-checked lock.

Bug: Two concurrent coroutines both pass the `if self._db is not None: return`
guard before either sets self._db (the assignment only happens after the await
suspension point). Both call aiosqlite.connect(), producing a leaked connection
and risking OperationalError from concurrent DDL on a WAL file.

Fix: async with self._lock + inner re-check in init().
Tracker: #768
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock

import pytest

from deile.core.session_store import SessionStore


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 session-store-concurrent-init — fix pending tracker #768",
)
async def test_concurrent_init_creates_exactly_one_connection(tmp_path) -> None:
    """Both concurrent init() calls must share a single aiosqlite connection.

    When the bug is present:
      - aiosqlite.connect() is called twice
      - The first connection is leaked (_running=True but not stored in _db)

    When fixed:
      - aiosqlite.connect() is called exactly once
    """
    connect_calls: list[object] = []
    real_connect = __import__("aiosqlite").connect

    async def counting_connect(path, **kwargs):
        conn = await real_connect(path, **kwargs)
        connect_calls.append(conn)
        return conn

    store = SessionStore(tmp_path / "sessions.sqlite")

    with mock.patch("aiosqlite.connect", side_effect=counting_connect):
        await asyncio.gather(store.init(), store.init())

    try:
        await store.close()
    except Exception:
        pass

    # When bug is present: connect_calls has 2 entries, test xfails (passes here).
    # When fixed: connect_calls has 1 entry, xpass causes strict=True to fail.
    assert len(connect_calls) == 1, (
        f"Expected exactly 1 aiosqlite.connect() call, got {len(connect_calls)}. "
        "First connection is leaked."
    )
