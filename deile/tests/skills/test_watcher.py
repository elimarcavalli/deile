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
from watchdog.events import (DirModifiedEvent, FileClosedEvent,
                             FileClosedNoWriteEvent, FileCreatedEvent,
                             FileDeletedEvent, FileModifiedEvent,
                             FileMovedEvent, FileOpenedEvent)

from deile.skills.registry import get_skill_registry, reset_skill_registry
from deile.skills.watcher import (SkillsWatcher, _DebounceWorker,
                                  reload_registry)


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


@pytest.mark.unit
class TestSkillsWatcher:
    # Injetam o callback do watcher diretamente (mesmo padrão de test_hot_loader),
    # sem depender de FSEvents reais nem de poll — determinístico e instantâneo.

    def test_creating_md_file_triggers_reload(self, tmp_path: Path) -> None:
        paths = _isolated(tmp_path)
        user_skills_dir = paths["user_home"] / ".deile" / "skills"
        user_skills_dir.mkdir(parents=True)
        # Trigger an initial scan so the registry baseline is non-empty.
        reload_registry(**paths)
        assert "fresh" not in get_skill_registry().list_names()

        watcher = SkillsWatcher(debounce_seconds=0.1, **paths)
        (user_skills_dir / "fresh.md").write_text(
            "---\nname: fresh\ndescription: Fresh\n---\nbody", encoding="utf-8"
        )
        # Injeta o reload que o watchdog dispararia, sem esperar o evento do SO.
        watcher._trigger_reload()

        assert "fresh" in get_skill_registry().list_names()


    @pytest.mark.integration
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
class TestWatcherEventFilter:
    # Handler REAL capturado de um Observer falso: determinístico em qualquer SO.

    @pytest.fixture
    def fire(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import watchdog.observers

        handlers: list = []

        class _FakeObserver:
            def schedule(self, handler, path, recursive=False):
                handlers.append(handler)

            def start(self): ...

            def stop(self): ...

            def join(self, timeout=None): ...

        monkeypatch.setattr(watchdog.observers, "Observer", _FakeObserver)
        watcher = SkillsWatcher(debounce_seconds=0.1, **_isolated(tmp_path))
        signals: list = []
        monkeypatch.setattr(watcher, "_on_event", signals.append)
        assert watcher.start()

        def _fire(event) -> list:
            handlers[0].on_any_event(event)
            return signals

        yield _fire
        watcher.stop()

    @pytest.mark.parametrize(
        "event",
        [FileOpenedEvent("/s/skill.md"), FileClosedNoWriteEvent("/s/skill.md")],
        ids=lambda e: e.event_type,
    )
    def test_read_only_events_do_not_reload(self, fire, event) -> None:
        # inotify emite abertura/leitura; como o reload LÊ os .md, reagir a elas o realimenta sem fim.
        assert fire(event) == []

    @pytest.mark.parametrize(
        "event",
        [
            FileCreatedEvent("/s/skill.md"),
            FileModifiedEvent("/s/skill.md"),
            FileDeletedEvent("/s/skill.md"),
            FileMovedEvent("/s/skill.md", "/s/renamed.md"),
            FileClosedEvent("/s/skill.md"),
        ],
        ids=lambda e: e.event_type,
    )
    def test_write_events_on_md_reload(self, fire, event) -> None:
        assert fire(event) == ["/s/skill.md"]

    def test_atomic_save_renaming_onto_md_reloads(self, fire) -> None:
        # sed -i e editores gravam num temporário e o renomeiam SOBRE o .md.
        assert fire(FileMovedEvent("/s/.skill.md.tmp", "/s/skill.md")) == ["/s/skill.md"]

    def test_non_md_and_directory_events_are_ignored(self, fire) -> None:
        fire(FileModifiedEvent("/s/notes.txt"))
        assert fire(DirModifiedEvent("/s/dir.md")) == []


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
