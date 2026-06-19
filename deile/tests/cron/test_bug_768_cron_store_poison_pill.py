"""xfail test for bug #768: CronStore._row_to_entry assert as poison-pill.

Bug: A single corrupt DB row (both cron and run_at NULL) causes AssertionError
inside a list comprehension in list_due(). The AssertionError propagates up to
CronRunner.tick()'s broad `except Exception`, returning 0 and silencing ALL
valid cron entries for that tick.

Fix: Replace assert with explicit CronStoreError; per-row try/except in list_due.
Tracker: #768
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from deile.cron.store import CronEntry, CronStore


@pytest.fixture()
def store_with_corrupt_row(tmp_path):
    """CronStore with one valid due entry and one corrupt row."""
    db_path = tmp_path / "cron_corrupt.db"
    store = CronStore(db_path)
    # Add a valid due entry
    store.add(
        CronEntry(
            id="valid-1",
            prompt="do something",
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    # Inject corrupt row directly via sqlite3 (both cron and run_at NULL)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cron_entries
                (id, prompt, cron, run_at, next_fire_at,
                 last_fire_at, last_result, error_count, enabled, created_at, deleted_at)
            VALUES
                ('corrupt-row', 'bad prompt', NULL, NULL,
                 '2026-01-01T00:00:00Z',
                 NULL, NULL, 0, 1, '2026-01-01T00:00:00Z', NULL)
            """
        )
        conn.commit()
    return store


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 cron-store-poison-pill — fix pending tracker #768",
)
def test_corrupt_row_does_not_silence_valid_entries(store_with_corrupt_row) -> None:
    """list_due() must return valid entries even when one row is corrupt.

    When the bug is present:
      - list_due() raises AssertionError (corrupt row acts as poison-pill)
      - No entries are returned, valid entry is silenced

    When fixed:
      - list_due() skips the corrupt row, returns the 1 valid entry
    """
    try:
        entries = store_with_corrupt_row.list_due()
    except AssertionError:
        # Bug present: AssertionError escapes list comprehension.
        # xfail condition: test body raises, pytest catches it as expected failure.
        raise

    valid_ids = [e.id for e in entries]
    assert "valid-1" in valid_ids, (
        f"Valid entry 'valid-1' was silenced by corrupt row. Got entries: {valid_ids}"
    )
    assert "corrupt-row" not in valid_ids, (
        "Corrupt row must be skipped, not returned as a CronEntry."
    )
