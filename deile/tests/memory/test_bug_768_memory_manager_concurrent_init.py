"""xfail test for bug #768: MemoryManager.initialize() missing asyncio.Lock.

Bug: Two concurrent coroutines both pass the `if self._is_initialized: return`
guard before either sets _is_initialized=True (every await between lines 115
and 133 is a preemption point). Each creates its own consolidation Task via
asyncio.create_task(). The second assignment overwrites self._consolidation_task,
orphaning the first task. shutdown() only cancels the second task; the first
continues calling optimize_memory() on torn-down components.

Fix: asyncio.Lock + double-checked locking in initialize().
Tracker: #768
"""

from __future__ import annotations

import asyncio

import pytest

from deile.memory.memory_manager import MemoryConfiguration, MemoryManager


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 memory-manager-concurrent-init — fix pending tracker #768",
)
async def test_concurrent_initialize_creates_exactly_one_consolidation_task(
    tmp_path,
) -> None:
    """Concurrent initialize() calls must produce exactly one consolidation task.

    When the bug is present:
      - Two consolidation tasks are created
      - The first is orphaned after shutdown()
      - Assertion len == 1 fails -> xfail

    When fixed:
      - Exactly one consolidation task is created
      - Assertion passes -> xpass
    """
    cfg = MemoryConfiguration(
        consolidation_interval=3600,  # non-zero: creates consolidation task
        working_memory_size=1024,
    )
    mm = MemoryManager(config=cfg, memory_dir=tmp_path)

    # Run both initialize() calls concurrently
    t1 = asyncio.create_task(mm.initialize())
    t2 = asyncio.create_task(mm.initialize())
    await asyncio.gather(t1, t2)

    # Count consolidation tasks by name pattern in all current tasks
    all_tasks = asyncio.all_tasks()
    consolidation_tasks = [
        t for t in all_tasks
        if "consolidation" in (t.get_name() or "").lower()
        and not t.done()
    ]

    # Cleanup
    await mm.shutdown()
    # Cancel any orphaned tasks
    for t in consolidation_tasks:
        if not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    assert len(consolidation_tasks) == 1, (
        f"Expected exactly 1 consolidation task after concurrent initialize(), "
        f"got {len(consolidation_tasks)}. "
        "Second call created a duplicate task that became orphaned on shutdown()."
    )
