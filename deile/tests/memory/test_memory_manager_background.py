"""Regression tests for the MemoryManager background-task lifecycle.

Bug: ``asyncio.create_task`` was used for fire-and-forget semantic
knowledge extraction and pattern analysis without keeping a hard
reference. Per the asyncio docs, the event loop only keeps a weak
reference to Task objects, so the GC could collect them mid-execution.
Under load, semantic_memory/procedural_memory writes were silently lost.

The fix holds tasks in ``self._background_tasks`` until done and surfaces
their exceptions via a done-callback instead of swallowing them.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from deile.memory.memory_manager import MemoryConfiguration, MemoryManager


def _new_manager(tmp_path) -> MemoryManager:
    cfg = MemoryConfiguration(
        working_memory_size=1024,
        consolidation_interval=0,  # don't start the consolidation loop
    )
    return MemoryManager(config=cfg, memory_dir=tmp_path)


async def test_spawn_background_keeps_task_referenced(tmp_path) -> None:
    mm = _new_manager(tmp_path)
    done = asyncio.Event()

    async def slow_work() -> None:
        # Yield to the loop multiple times so a missing strong-ref would
        # plausibly leave the task in a state the GC could finalize.
        for _ in range(3):
            await asyncio.sleep(0)
        done.set()

    task = mm._spawn_background(slow_work(), name="slow_work")
    # Strong ref must be in the manager's set.
    assert task in mm._background_tasks

    # Force GC — must NOT collect the running task.
    gc.collect()
    assert task in mm._background_tasks
    assert not task.done()

    await done.wait()
    # Let the done-callback run.
    await asyncio.sleep(0)
    assert task not in mm._background_tasks


async def test_spawn_background_logs_exception(tmp_path, caplog) -> None:
    mm = _new_manager(tmp_path)
    caplog.set_level("ERROR")

    async def broken() -> None:
        raise RuntimeError("kaboom")

    task = mm._spawn_background(broken(), name="broken")
    # Wait until done.
    try:
        await task
    except RuntimeError:
        pass
    # Done-callback yields control once for cleanup.
    await asyncio.sleep(0)

    assert task not in mm._background_tasks
    assert any("Background task" in rec.message for rec in caplog.records)
    assert any("kaboom" in rec.message or "kaboom" in str(rec.exc_info) for rec in caplog.records)


async def test_spawn_background_cancelled_task_does_not_log_error(
    tmp_path, caplog
) -> None:
    mm = _new_manager(tmp_path)
    caplog.set_level("ERROR")

    async def forever() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    task = mm._spawn_background(forever(), name="forever")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    assert task not in mm._background_tasks
    # No error log expected on cancellation.
    error_msgs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert not error_msgs, [r.message for r in error_msgs]
