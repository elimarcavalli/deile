"""Tests for the SkillsWatcher hot-reload pipeline.

These tests exercise ``reload_registry`` directly (deterministic, no I/O
timing) and use a low-level integration test on the watcher with a tiny
debounce window. They avoid sleeping for long periods by polling.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from deile.skills.registry import get_skill_registry, reset_skill_registry
from deile.skills.watcher import _DebounceWorker, SkillsWatcher, reload_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


def _isolated(tmp_path: Path) -> dict:
    """Return a dict of paths suitable for passing to the reload/watcher APIs.

    Isolates project_dir + user_home so the developer's real
    ``~/.deile/skills/`` does not leak into the test.
    """
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return {"project_dir": project, "user_home": home}


@pytest.mark.unit
class TestReloadRegistry:
    def test_new_file_appears_after_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        # Pre-state: only the bundled skills are loaded.
        reload_registry(**paths)
        before = set(get_skill_registry().list_names())
        assert "extra-one" not in before

        # Drop a file into the user dir and re-reload.
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "extra-one.md").write_text(
            "---\nname: extra-one\ndescription: Extra\n---\nbody", encoding="utf-8"
        )

        count = reload_registry(**paths)
        assert count == len(before) + 1
        assert "extra-one" in get_skill_registry().list_names()

    def test_deleted_file_disappears_after_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        target = user_skills_dir / "ephemeral.md"
        target.write_text(
            "---\nname: ephemeral\ndescription: Will be deleted\n---\nbody", encoding="utf-8"
        )

        reload_registry(**paths)
        assert "ephemeral" in get_skill_registry().list_names()

        target.unlink()
        reload_registry(**paths)
        assert "ephemeral" not in get_skill_registry().list_names()

    def test_edited_body_takes_effect_after_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        target = user_skills_dir / "rewritable.md"
        target.write_text(
            "---\nname: rewritable\n---\nVERSION ONE", encoding="utf-8"
        )

        reload_registry(**paths)
        assert get_skill_registry().get("rewritable").body == "VERSION ONE"

        target.write_text(
            "---\nname: rewritable\n---\nVERSION TWO", encoding="utf-8"
        )
        reload_registry(**paths)
        assert get_skill_registry().get("rewritable").body == "VERSION TWO"

    def test_command_registry_refresh(self, tmp_path: Path) -> None:
        from deile.commands.registry import CommandRegistry

        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "v1.md").write_text(
            "---\nname: v1\n---\nbody-v1", encoding="utf-8"
        )

        cmd_registry = CommandRegistry()
        reload_registry(command_registry=cmd_registry, **paths)
        assert cmd_registry.get_command("v1") is not None

        # Replace v1 with v2 on disk and reload — v1 should be gone.
        (user_skills_dir / "v1.md").unlink()
        (user_skills_dir / "v2.md").write_text(
            "---\nname: v2\n---\nbody-v2", encoding="utf-8"
        )
        reload_registry(command_registry=cmd_registry, **paths)
        assert cmd_registry.get_command("v1") is None
        assert cmd_registry.get_command("v2") is not None


@pytest.mark.integration
class TestSkillsWatcher:
    def _wait_for(self, predicate, timeout: float = 5.0, poll: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return False

    def test_creating_md_file_triggers_reload(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        # Trigger an initial scan so the registry baseline is non-empty.
        reload_registry(**paths)

        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        try:
            assert watcher.start() is True

            (user_skills_dir / "fresh.md").write_text(
                "---\nname: fresh\ndescription: Fresh\n---\nbody", encoding="utf-8"
            )

            assert self._wait_for(
                lambda: "fresh" in get_skill_registry().list_names()
            ), "watcher never picked up the new file"
        finally:
            watcher.stop()

    def test_modifying_md_file_triggers_reload(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        target = user_skills_dir / "mutable.md"
        target.write_text(
            "---\nname: mutable\n---\nold body", encoding="utf-8"
        )
        reload_registry(**paths)
        assert get_skill_registry().get("mutable").body == "old body"

        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        try:
            assert watcher.start() is True
            target.write_text(
                "---\nname: mutable\n---\nnew body", encoding="utf-8"
            )
            assert self._wait_for(
                lambda: get_skill_registry().get("mutable").body == "new body"
            )
        finally:
            watcher.stop()

    def test_non_md_files_are_ignored(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        reload_registry(**paths)
        baseline = set(get_skill_registry().list_names())

        reloads: list = []
        watcher = SkillsWatcher(
            debounce_seconds=0.1,
            on_reload=lambda count: reloads.append(count),
            **paths,
        )
        try:
            assert watcher.start() is True
            # Drop a non-.md file — should NOT trigger a reload.
            (user_skills_dir / "notes.txt").write_text("nope", encoding="utf-8")
            time.sleep(0.5)  # well past debounce
            assert reloads == [], f"reload fired for non-.md file: {reloads}"
            # Registry contents should be unchanged.
            assert set(get_skill_registry().list_names()) == baseline
        finally:
            watcher.stop()

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        pytest.importorskip("watchdog")
        paths = _isolated(tmp_path)
        (paths["user_home"] / ".deile" / "skills").mkdir(parents=True)
        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        watcher.start()
        watcher.stop()
        watcher.stop()  # second stop should not raise
        assert watcher.is_active is False


@pytest.mark.unit
class TestDebounceWorkerNoThreadLeak:
    """Verify that rapid FS events do NOT accumulate OS threads.

    Regression test for the bug reported on the deile-monitor pod after ~22h
    uptime: ``RuntimeError: can't start new thread``.  The old implementation
    created a new ``threading.Timer`` per event; under heavy load (large git
    checkout, slow ``reload_registry``, etc.) cancelled-but-not-yet-exited
    timer threads accumulated until the OS ulimit was hit.

    The new implementation uses a single ``_DebounceWorker`` thread — firing
    1000 signals must NOT create more than 1 additional thread.
    """

    def test_single_worker_thread_survives_burst(self) -> None:
        """1000 rapid signals must not spawn more than 1 extra thread."""
        reload_calls: list = []
        barrier = threading.Event()

        def slow_trigger() -> None:
            reload_calls.append(1)
            # Simulate a slow reload to maximise thread-accumulation pressure.
            barrier.wait(timeout=2.0)

        before = threading.active_count()
        worker = _DebounceWorker(debounce_seconds=0.01, trigger=slow_trigger)
        worker.start()

        try:
            # Fire 1000 signals in rapid succession.
            for _ in range(1000):
                worker.signal()

            # Give debounce window time to elapse so trigger fires.
            time.sleep(0.1)

            peak = threading.active_count()
            # Allow the slow trigger to finish.
            barrier.set()
            time.sleep(0.1)
        finally:
            worker.stop()

        # The worker itself is 1 extra thread; the watchdog observer is not
        # started here.  Allow a small slack of 2 for any test-framework
        # threads that might appear briefly.
        extra = peak - before
        assert extra <= 3, (
            f"Thread leak detected: {extra} extra threads at peak "
            f"(expected ≤ 3 for single debounce worker). "
            f"before={before}, peak={peak}"
        )

    def test_many_signals_produce_single_reload(self) -> None:
        """A burst of signals within the debounce window fires exactly one reload."""
        reload_calls: list = []

        def trigger() -> None:
            reload_calls.append(time.monotonic())

        worker = _DebounceWorker(debounce_seconds=0.05, trigger=trigger)
        worker.start()
        try:
            # Send 50 signals in quick succession — all within debounce window.
            for _ in range(50):
                worker.signal()
            # Wait for 3× the debounce window so the reload must have fired.
            time.sleep(0.3)
        finally:
            worker.stop()

        assert len(reload_calls) == 1, (
            f"Expected exactly 1 reload from burst of 50 signals, "
            f"got {len(reload_calls)}: {reload_calls}"
        )

    def test_stop_cleans_up_worker_thread(self) -> None:
        """After stop(), the worker thread is no longer alive."""
        worker = _DebounceWorker(debounce_seconds=0.1, trigger=lambda: None)
        worker.start()
        assert worker.is_alive()
        worker.stop()
        assert not worker.is_alive(), "Worker thread still alive after stop()"


@pytest.mark.unit
class TestReloadSerialization:
    def test_reload_registry_uses_atomic_swap(self, tmp_path: Path) -> None:
        # Two threads call reload_registry concurrently — the second blocks on
        # the lock so the registry never has a torn state. Proxy assertion:
        # after both reloads complete, the expected skills are present.
        user_skills_dir = tmp_path / "home" / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "z.md").write_text(
            "---\nname: z\n---\nbody", encoding="utf-8"
        )

        def go() -> None:
            for _ in range(20):
                reload_registry(
                    project_dir=tmp_path / "project",
                    user_home=tmp_path / "home",
                )

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        names = set(get_skill_registry().list_names())
        # 'z' from disk plus the bundled skills (python/typescript/tdd).
        assert "z" in names
        assert {"python", "typescript", "tdd"}.issubset(names)
