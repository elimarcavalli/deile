"""Regression tests for ``MemoryConsolidator.consolidate_all``.

Bug: the function called ``get_stats()`` first to read ``entries_before``.
``get_stats()`` already runs ``_cleanup_expired()`` internally, so by the
time the consolidator's own ``_cleanup_expired()`` call ran, there was
nothing left to clean — ``expired_cleaned`` was always 0 and
``entries_before`` reflected the POST-cleanup count, not the pre-cleanup
count. The value is surfaced to users via ``/memory compact``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from deile.memory.memory_consolidation import MemoryConsolidator
from deile.memory.working_memory import WorkingMemory


async def _populate(wm: WorkingMemory, *, alive: int, expired: int) -> None:
    """Add ``alive`` long-lived entries and ``expired`` already-expired ones."""
    import time

    for i in range(alive):
        await wm.store(f"alive-{i}", ttl=3600)
    for i in range(expired):
        entry_id = await wm.store(f"expired-{i}", ttl=1)
        # Backdate the timestamp so the TTL has already elapsed by the time
        # _cleanup_expired runs (``ttl=0`` is treated as "use default").
        wm._entries[entry_id].timestamp = time.time() - 3600


async def test_consolidate_all_reports_actual_expired_cleaned(tmp_path) -> None:
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    # Skip starting the cleanup background task; we want full control.
    wm._is_initialized = True

    await _populate(wm, alive=3, expired=4)

    consolidator = MemoryConsolidator(
        working_memory=wm,
        episodic_memory=MagicMock(),
        semantic_memory=MagicMock(),
        procedural_memory=MagicMock(),
    )

    report = await consolidator.consolidate_all()

    assert "error" not in report, report
    assert report["working_memory"]["entries_before"] == 7
    assert report["working_memory"]["expired_cleaned"] == 4
    assert report["working_memory"]["entries_after"] == 3


async def test_consolidate_all_no_expired_keeps_count(tmp_path) -> None:
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    await _populate(wm, alive=5, expired=0)

    consolidator = MemoryConsolidator(
        working_memory=wm,
        episodic_memory=MagicMock(),
        semantic_memory=MagicMock(),
        procedural_memory=MagicMock(),
    )

    report = await consolidator.consolidate_all()

    assert report["working_memory"]["entries_before"] == 5
    assert report["working_memory"]["expired_cleaned"] == 0
    assert report["working_memory"]["entries_after"] == 5


async def test_consolidate_all_with_only_expired_drains() -> None:
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    await _populate(wm, alive=0, expired=6)

    consolidator = MemoryConsolidator(
        working_memory=wm,
        episodic_memory=MagicMock(),
        semantic_memory=MagicMock(),
        procedural_memory=MagicMock(),
    )

    report = await consolidator.consolidate_all()

    assert report["working_memory"]["entries_before"] == 6
    assert report["working_memory"]["expired_cleaned"] == 6
    assert report["working_memory"]["entries_after"] == 0


async def test_cleanup_loop_sleeps_on_error_to_avoid_hot_loop() -> None:
    """Persistent failures in _cleanup_expired must not spin the loop hot.

    The fix wraps the error path in a 60s sleep. We patch cleanup to fail
    once, then succeed, and measure that the loop actually paused.
    """
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    call_count = 0
    original_cleanup = wm._cleanup_expired

    async def flaky_cleanup() -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated transient failure")
        return await original_cleanup()

    wm._cleanup_expired = flaky_cleanup

    # Patch sleep to fast-forward and verify both the 300s and 60s pauses
    # are honoured.
    sleep_calls: list[float] = []
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        # Cancel after we've observed at least one 60s recovery sleep.
        if 60 in sleep_calls:
            raise asyncio.CancelledError()
        # Yield so the loop can continue.
        await original_sleep(0)

    import deile.memory.working_memory as wm_mod

    saved = wm_mod.asyncio.sleep
    wm_mod.asyncio.sleep = fast_sleep
    try:
        await wm._cleanup_loop()
    finally:
        wm_mod.asyncio.sleep = saved

    # The first 300s wait runs cleanup (which fails); the 60s recovery sleep
    # must appear in the call log — that's the fix.
    assert 300 in sleep_calls
    assert 60 in sleep_calls
