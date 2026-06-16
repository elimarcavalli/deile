"""Regression tests for the plugin hot-reload watcher.

Historically ``PluginFileHandler.on_modified`` called ``asyncio.create_task``
directly. That handler runs on a watchdog worker thread (``Observer`` extends
``Thread``), where no event loop is running — ``create_task`` raised
``RuntimeError`` and was silently swallowed by watchdog, so reload coroutines
were never scheduled and the documented hot-reload feature was dead.

These tests verify the handler now hops back onto the loop captured by
``HotLoader.start`` via ``run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from deile.plugins.hot_loader import HotLoader, PluginFileHandler


class _FakePluginManager:
    def __init__(self, plugins_dir):
        self.plugins_dir = plugins_dir
        self.reload_calls: list[str] = []
        self._reload_event = asyncio.Event()

    async def reload_plugin(self, plugin_id: str) -> None:
        self.reload_calls.append(plugin_id)
        self._reload_event.set()

    async def wait_for_reload(self, timeout: float = 2.0) -> bool:
        try:
            await asyncio.wait_for(self._reload_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


async def test_on_modified_schedules_reload_from_worker_thread(tmp_path) -> None:
    """The cross-thread bridge must schedule the coroutine on the loop."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "my_plugin"
    plugin_dir.mkdir(parents=True)
    changed_file = plugin_dir / "tool.py"
    changed_file.write_text("# noop\n")

    pm = _FakePluginManager(plugins_dir)
    loop = asyncio.get_running_loop()
    handler = PluginFileHandler(pm, loop)

    event = SimpleNamespace(is_directory=False, src_path=str(changed_file))

    # Fire the handler from a real worker thread (mirrors watchdog behaviour).
    worker_done = threading.Event()

    def fire() -> None:
        handler.on_modified(event)
        worker_done.set()

    threading.Thread(target=fire).start()
    # Wait for the worker to publish the schedule call.
    assert await asyncio.get_running_loop().run_in_executor(None, worker_done.wait, 2.0)

    assert await pm.wait_for_reload(timeout=2.0)
    assert pm.reload_calls == ["my_plugin"]


async def test_on_modified_ignores_when_loop_is_closed(tmp_path) -> None:
    """A closed loop must not raise; the coroutine should be closed cleanly."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "my_plugin"
    plugin_dir.mkdir(parents=True)
    changed_file = plugin_dir / "tool.py"
    changed_file.write_text("# noop\n")

    pm = MagicMock()
    pm.plugins_dir = plugins_dir
    closed_coro_holder = {}

    async def _fake_reload(plugin_id):  # captured to verify it gets closed
        closed_coro_holder["called"] = True

    pm.reload_plugin = _fake_reload

    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    handler = PluginFileHandler(pm, closed_loop)
    event = SimpleNamespace(is_directory=False, src_path=str(changed_file))

    # Must not raise even though loop is closed.
    handler.on_modified(event)
    # The coroutine should never have been awaited (loop was closed).
    assert "called" not in closed_coro_holder


async def test_hot_loader_start_captures_loop(tmp_path) -> None:
    """HotLoader.start must capture the running loop for cross-thread dispatch."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    pm = MagicMock()
    pm.plugins_dir = plugins_dir
    loader = HotLoader(pm)

    await loader.start()
    try:
        assert loader._loop is asyncio.get_running_loop()
        assert loader._is_active is True
    finally:
        await loader.stop()
        assert loader._loop is None
