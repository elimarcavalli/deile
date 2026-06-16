"""File-system watcher that reloads the unified ``SkillRegistry`` in place.

Concurrency:
- ``watchdog`` runs the observer on its own thread.
- Editor write-bursts are debounced by a **single, reused** ``_DebounceWorker``
  thread (replaces the old per-event ``threading.Timer`` pattern that leaked
  threads when the reload was slow or FS events arrived in bursts).
- ``_DebounceWorker`` wakes on ``_event`` (set by ``_on_event``), checks whether
  the debounce window has elapsed, and loops.  The loop exits when ``_stopping``
  is True.  At most **one** extra thread is alive regardless of event rate.
- ``reload_registry`` swaps registry contents atomically via
  ``SkillRegistry.replace_all`` so readers never see a torn state.
- ``_RELOAD_LOCK`` serializes overlapping reloads (manual + watcher; or two
  watcher events while a rescan is still running).
- ``stop()`` is idempotent and joins the observer thread before returning.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from .discovery import default_scan_order, discover_skills_sync
from .registry import get_skill_registry
from .slash_command_bridge import register_skills_as_commands, unregister_skill_commands

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 0.5

# Process-wide lock — keeps overlapping reloads from racing on the registry swap.
_RELOAD_LOCK = threading.Lock()


def reload_registry(
    *,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    command_registry: Any = None,
) -> int:
    """Rescan every configured directory and overwrite the registry atomically.

    Returns the number of skills now in the registry.
    """
    with _RELOAD_LOCK:
        skills, _overrides = discover_skills_sync(
            project_dir=project_dir,
            user_home=user_home,
            extra_paths=extra_paths,
        )
        registry = get_skill_registry()
        registry.replace_all(skills)

        if command_registry is not None:
            unregister_skill_commands(command_registry)
            invocable = [s for s in skills if s.source != "bundled"]
            register_skills_as_commands(invocable, command_registry)

        logger.info("skills: hot-reload — registry now holds %d skill(s)", len(skills))
        return len(skills)


class _DebounceWorker(threading.Thread):
    """Single background thread that fires a reload after a quiet period.

    Design rationale
    ----------------
    The previous implementation created a new ``threading.Timer`` (= a new OS
    thread) on *every* filesystem event and cancelled the previous one.  Under
    heavy load — e.g. a large ``git checkout``, a language server touching many
    ``.md`` files at once, or a slow ``reload_registry`` call that holds
    ``_RELOAD_LOCK`` while many new events arrive — cancelled timers accumulated
    faster than Python's GC reclaimed their thread objects, eventually hitting
    the OS ``ulimit -u`` ceiling and raising ``RuntimeError: can't start new
    thread``.

    This class replaces all of that with **one** long-lived thread.  The thread
    blocks on an ``threading.Event``; ``signal()`` wakes it and records the
    "last seen" timestamp.  The thread loops: if the debounce window has not
    elapsed yet it waits the remaining time and checks again; once the window
    elapses without a new signal it fires the reload and goes back to sleep.

    At most **one** extra OS thread is alive regardless of how many FS events
    arrive per second.
    """

    def __init__(
        self,
        debounce_seconds: float,
        trigger: Callable[[], None],
    ) -> None:
        super().__init__(daemon=True, name="deile-skills-debounce")
        self._debounce = debounce_seconds
        self._trigger = trigger
        self._event = threading.Event()
        self._stop_event = threading.Event()
        # Monotonic timestamp of the last signal; 0 = no pending signal.
        self._last_signal: float = 0.0
        self._lock = threading.Lock()

    def signal(self) -> None:
        """Record a new FS event and wake the worker thread."""
        with self._lock:
            self._last_signal = time.monotonic()
        self._event.set()

    def stop(self) -> None:
        """Request shutdown and join."""
        self._stop_event.set()
        self._event.set()  # unblock the wait
        self.join(timeout=2.0)

    def run(self) -> None:  # noqa: C901 (acceptable complexity for a loop)
        while not self._stop_event.is_set():
            self._event.wait()
            self._event.clear()

            if self._stop_event.is_set():
                break

            # Drain the debounce window: keep waiting until no new signal
            # arrives for the full debounce interval.
            while True:
                with self._lock:
                    last = self._last_signal
                remaining = last + self._debounce - time.monotonic()
                if remaining <= 0:
                    break
                # Sleep for the remaining window; a new signal will set _event
                # again and we'll recheck.
                self._event.wait(timeout=remaining)
                self._event.clear()
                if self._stop_event.is_set():
                    return

            # Clear last_signal so we don't re-fire until the next signal.
            with self._lock:
                self._last_signal = 0.0

            if not self._stop_event.is_set():
                self._trigger()


class SkillsWatcher:
    """Filesystem watcher that calls ``reload_registry`` on ``.md`` changes."""

    def __init__(
        self,
        *,
        project_dir: Optional[Path] = None,
        user_home: Optional[Path] = None,
        extra_paths: Iterable[Path] = (),
        command_registry: Any = None,
        debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
        on_reload: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._project_dir = project_dir
        self._user_home = user_home
        self._extra_paths: List[Path] = list(extra_paths)
        self._command_registry = command_registry
        self._debounce_seconds = max(0.05, debounce_seconds)
        self._on_reload = on_reload
        self._on_error = on_error

        self._observer: Optional[Any] = None
        self._debounce_worker: Optional[_DebounceWorker] = None
        self._is_active = False
        self._stopping = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    def start(self) -> bool:
        """Begin watching every existing directory in the scan order.

        Returns False (and logs a warning) when ``watchdog`` is missing or no
        scan-order directory exists yet — so a misconfigured environment is
        immediately visible without crashing startup.
        """
        if self._is_active:
            return True

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "skills: hot-reload disabled — `watchdog` is not installed; "
                "skill edits will require an agent restart to take effect"
            )
            return False

        scan_entries = default_scan_order(
            project_dir=self._project_dir,
            user_home=self._user_home,
            extra_paths=self._extra_paths,
        )
        observable_dirs = [
            entry.directory for entry in scan_entries if entry.directory.is_dir()
        ]
        if not observable_dirs:
            missing = [entry.directory for entry in scan_entries]
            logger.warning(
                "skills: hot-reload not started — none of the %d configured "
                "skill directories exists yet: %s",
                len(scan_entries),
                ", ".join(str(p) for p in missing),
            )
            return False

        # Start the single debounce worker thread before scheduling watchdog.
        worker = _DebounceWorker(
            debounce_seconds=self._debounce_seconds,
            trigger=self._trigger_reload,
        )
        worker.start()
        self._debounce_worker = worker

        callback = self._on_event

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory or not str(event.src_path).endswith(".md"):
                    return
                callback(event.src_path)

        handler = _Handler()
        observer = Observer()
        scheduled_count = 0
        for directory in observable_dirs:
            try:
                observer.schedule(handler, str(directory), recursive=True)
                scheduled_count += 1
            except Exception as exc:
                logger.warning(
                    "skills: cannot watch %s for hot-reload (%s); changes there "
                    "will be ignored until restart",
                    directory,
                    exc,
                )

        if scheduled_count == 0:
            logger.warning(
                "skills: hot-reload not started — every observer.schedule() call failed"
            )
            worker.stop()
            self._debounce_worker = None
            return False

        observer.start()
        self._observer = observer
        self._is_active = True
        logger.info("skills: hot-reload watching %d directory/ies", scheduled_count)
        return True

    def stop(self) -> None:
        """Idempotent — stops the debounce worker and joins the observer."""
        self._stopping = True
        if self._debounce_worker is not None:
            self._debounce_worker.stop()
            self._debounce_worker = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception as exc:
                logger.warning("skills: error stopping watcher: %s", exc)
            self._observer = None
        self._is_active = False

    def _on_event(self, src_path: str) -> None:
        if self._stopping:
            return
        if self._debounce_worker is not None:
            self._debounce_worker.signal()

    def _trigger_reload(self) -> None:
        if self._stopping:
            return
        try:
            count = reload_registry(
                project_dir=self._project_dir,
                user_home=self._user_home,
                extra_paths=self._extra_paths,
                command_registry=self._command_registry,
            )
        except Exception as exc:
            logger.warning(
                "skills: hot-reload failed (%s: %s) — registry kept the prior state",
                type(exc).__name__,
                exc,
            )
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except Exception as cb_exc:
                    logger.warning("skills: on_error callback raised: %s", cb_exc)
            return
        if self._on_reload is not None:
            try:
                self._on_reload(count)
            except Exception as exc:
                logger.warning("skills: on_reload callback raised: %s", exc)
