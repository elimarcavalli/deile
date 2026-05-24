"""File-system watcher that reloads the unified ``SkillRegistry`` in place.

When DEILE is running interactively, dropping a new ``*.md`` into one of
the skill directories (or editing/removing an existing one) should make the
skill available on the very next turn — no agent restart required.

The watcher is built on top of the **same** scan order that the
``bootstrap_skills`` flow uses (``default_scan_order()``), so any path
configured for discovery is automatically picked up — no hardcoded path
list lives in this module.

Concurrency model:

- ``watchdog`` runs the file-system observer on its own thread.
- ``SkillsWatcher`` debounces bursts of editor write events (many editors
  emit multiple writes per save) into a single reload pass via a
  ``threading.Timer`` guarded by ``_timer_lock``.
- ``reload_registry`` swaps the singleton ``SkillRegistry`` contents
  atomically using ``SkillRegistry.replace_all`` so a reader never sees an
  empty / partially-populated state.
- A dedicated ``_reload_lock`` serializes overlapping reloads (a long
  rescan + a fresh event arriving before it completes).
- ``stop()`` is idempotent and joins the observer thread before returning.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from .discovery import default_scan_order, discover_skills_sync
from .registry import get_skill_registry
from .slash_command_bridge import (
    register_skills_as_commands,
    unregister_skill_commands,
)

logger = logging.getLogger(__name__)


# Coalesce editor write-bursts into a single reload.
_DEFAULT_DEBOUNCE_SECONDS = 0.5

# Process-wide lock for ``reload_registry`` — keeps overlapping reload
# requests (manual + watcher; or two watcher events while one rescan is
# still running) from racing on the registry swap.
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

    Thread-safe: serialized by ``_RELOAD_LOCK`` and the registry swap uses
    ``SkillRegistry.replace_all`` (one ``RLock`` acquisition) so concurrent
    readers either see the full old state or the full new state — never a
    half-cleared registry. Refreshing the slash-command bridge (when
    *command_registry* is provided) happens under the same outer lock.
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
        self._timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        self._is_active = False
        self._stopping = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    def start(self) -> bool:
        """Begin watching every existing directory in the scan order.

        Returns True when at least one directory was scheduled. False when
        ``watchdog`` is not installed or no scan-order directory exists yet
        (in which case startup is a no-op — the watcher does nothing rather
        than waiting on directories that may never appear). Both failure
        cases log a clear warning so a misconfigured environment is
        immediately visible in the logs.
        """
        if self._is_active:
            return True

        try:
            from watchdog.events import FileSystemEventHandler  # noqa: F401 — availability check only
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
        observable_dirs = [entry.directory for entry in scan_entries if entry.directory.is_dir()]
        missing_dirs = [entry.directory for entry in scan_entries if not entry.directory.is_dir()]

        if not observable_dirs:
            logger.warning(
                "skills: hot-reload not started — none of the %d configured "
                "skill directories exists yet: %s",
                len(scan_entries),
                ", ".join(str(p) for p in missing_dirs),
            )
            return False

        handler = _make_event_handler(self._on_event)
        scheduled_count = 0
        observer = Observer()
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
            return False

        observer.start()
        self._observer = observer
        self._is_active = True
        logger.info("skills: hot-reload watching %d directory/ies", scheduled_count)
        return True

    def stop(self) -> None:
        """Stop the observer and cancel any pending debounce timer.

        Idempotent — safe to call multiple times.
        """
        self._stopping = True
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception as exc:
                logger.warning("skills: error stopping watcher: %s", exc)
            self._observer = None
        self._is_active = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_event(self, src_path: str) -> None:
        """Schedule (or reschedule) a debounced reload."""
        if self._stopping:
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._trigger_reload)
            self._timer.daemon = True
            self._timer.start()

    def _trigger_reload(self) -> None:
        # Clear the timer reference first so a fresh event arriving while
        # we run can start a new timer without races.
        with self._timer_lock:
            self._timer = None
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


def _make_event_handler(callback):
    """Build a watchdog event handler that fires *callback* on every ``.md`` event.

    Kept module-level so importing this module without ``watchdog`` installed
    only fails when ``SkillsWatcher.start()`` actually runs.
    """
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory:
                return
            if not str(event.src_path).endswith(".md"):
                return
            callback(event.src_path)

    return _Handler()
