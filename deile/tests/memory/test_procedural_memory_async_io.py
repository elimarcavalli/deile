"""Regression tests for ``ProceduralMemory`` blocking I/O on the event loop.

Bug: ``initialize()`` used a synchronous ``open()`` + ``json.load`` and
``shutdown()`` a synchronous ``open()`` + ``json.dump`` directly inside the
``async def`` bodies — blocking the event loop (princípio 03 §1). The
sibling ``SemanticMemory`` already offloads identical JSON I/O via
``asyncio.to_thread``; ``ProceduralMemory`` was left behind.

The fix routes both paths through ``deile.storage.aio_fileio`` (which wraps
the blocking calls in ``asyncio.to_thread``). These tests assert (a) the
round-trip still works and the on-disk format is preserved, and (b) the
JSON I/O is delegated to a worker thread (no blocking ``open`` on the loop).
"""

from __future__ import annotations

import json
from pathlib import Path

from deile.memory.procedural_memory import ProceduralMemory


async def test_patterns_round_trip_through_disk(tmp_path: Path) -> None:
    """Patterns survive a shutdown/initialize cycle via the JSONL store."""
    pm = ProceduralMemory(storage_dir=tmp_path, min_frequency=1)
    await pm.initialize()

    await pm.analyze_interaction({"input_length": 120})
    await pm.analyze_interaction({"input_length": 130})  # same 100-bucket key

    await pm.shutdown()

    # File written with the expected human-readable format (indent=2).
    patterns_file = tmp_path / "patterns.json"
    assert patterns_file.exists()
    on_disk = json.loads(patterns_file.read_text(encoding="utf-8"))
    assert on_disk["input_len_100"]["frequency"] == 2

    # A fresh instance re-hydrates the persisted patterns.
    pm2 = ProceduralMemory(storage_dir=tmp_path, min_frequency=1)
    await pm2.initialize()
    relevant = await pm2.get_relevant_patterns("anything")
    assert any(
        p["pattern"] == "input_len_100" and p["frequency"] == 2 for p in relevant
    )
    await pm2.shutdown()


async def test_initialize_offloads_blocking_read(tmp_path: Path, monkeypatch) -> None:
    """``initialize`` must route the JSON read through ``asyncio.to_thread``."""
    pm = ProceduralMemory(storage_dir=tmp_path, min_frequency=1)
    await pm.analyze_interaction({"input_length": 50})
    await pm.shutdown()
    assert (tmp_path / "patterns.json").exists()

    import deile.storage.aio_fileio as aio

    to_thread_called = False
    original = aio.asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        nonlocal to_thread_called
        to_thread_called = True
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(aio.asyncio, "to_thread", spy_to_thread)

    pm2 = ProceduralMemory(storage_dir=tmp_path, min_frequency=1)
    await pm2.initialize()
    assert to_thread_called, "initialize() must offload the blocking read to a thread"
    await pm2.shutdown()


async def test_shutdown_offloads_blocking_write(tmp_path: Path, monkeypatch) -> None:
    """``shutdown`` must route the JSON write through ``asyncio.to_thread``."""
    pm = ProceduralMemory(storage_dir=tmp_path, min_frequency=1)
    await pm.initialize()
    await pm.analyze_interaction({"input_length": 200})

    import deile.storage.aio_fileio as aio

    to_thread_called = False
    original = aio.asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        nonlocal to_thread_called
        to_thread_called = True
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(aio.asyncio, "to_thread", spy_to_thread)

    await pm.shutdown()
    assert to_thread_called, "shutdown() must offload the blocking write to a thread"


async def test_initialize_tolerates_corrupt_patterns_file(tmp_path: Path) -> None:
    """A corrupt ``patterns.json`` must not crash initialize(); start from empty."""
    (tmp_path / "patterns.json").write_text("{ not valid json !!!", encoding="utf-8")

    pm = ProceduralMemory(storage_dir=tmp_path, min_frequency=1)
    await pm.initialize()  # must not raise

    assert pm._is_initialized
    # No patterns loaded from the corrupt file.
    relevant = await pm.get_relevant_patterns("anything")
    assert relevant == []
