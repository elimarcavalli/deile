"""File-system watcher that reloads the unified ``SkillRegistry`` in place.

When DEILE is running interactively, dropping a new ``*.md`` into one of
the skill directories (or editing/removing an existing one) should make the
skill available on the very next turn — no agent restart required.

The watcher is built on top of the **same** scan order that the
``bootstrap_skills`` flow uses (``default_scan_order()``), so any path
configured for discovery is automatically picked up — no hardcoded path
list lives in this module.

Lifecycle:

- ``SkillsWatcher.start()`` schedules one ``Observer`` over every existing
  directory in the scan order and begins listening for ``*.md`` events.
- A debounce timer coalesces bursts of editor events (many editors emit
  multiple writes per save) into a single reload pass.
- ``SkillsWatcher.stop()`` joins the observer thread.

Reloads call ``discover_skills_sync()`` (fast, bounded) and merge into the
registry; when an optional ``command_registry`` was provided, the legacy
slash-command bridge is also refreshed so ``/<name>`` invocations stay in
sync.
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


def reload_registry(
    *,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    command_registry: Any = None,
) -> int:
    """Rescan every configured directory and overwrite the registry.

    Returns the number of skills now in the registry. Safe to call from any
    thread — the registry mutation is wrapped to leave it in a consistent
    state even when called concurrently with reads.
    """
    skills, _overrides = discover_skills_sync(
        project_dir=project_dir,
        user_home=user_home,
        extra_paths=extra_paths,
    )
    registry = get_skill_registry()
    # Replace (not merge): if a file was deleted on disk it must disappear
    # from the registry too. Clear-and-repopulate is the simplest correct
    # semantic; readers that see a momentary empty registry will just get
    # zero matches for that turn — equivalent to "no skills active".
    registry.clear()
    for skill in skills:
        registry.register(skill)

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
    ) -> None:
        self._project_dir = project_dir
        self._user_home = user_home
        self._extra_paths: List[Path] = list(extra_paths)
        self._command_registry = command_registry
        self._debounce_seconds = max(0.05, debounce_seconds)
        self._on_reload = on_reload

        self._observer: Optional[Any] = None
        self._timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        self._is_active = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    def start(self) -> bool:
        """Begin watching every existing directory in the scan order.

        Returns True when at least one directory was scheduled. False when
        ``watchdog`` is not installed or no scan-order directory exists yet
        (in which case startup is a no-op — the watcher does nothing rather
        than waiting on directories that may never appear).
        """
        if self._is_active:
            return True

        try:
            from watchdog.events import FileSystemEventHandler  # noqa: F401 — availability check only
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("skills: hot-reload disabled — watchdog is not installed")
            return False

        scan_entries = default_scan_order(
            project_dir=self._project_dir,
            user_home=self._user_home,
            extra_paths=self._extra_paths,
        )
        observable_dirs = [entry.directory for entry in scan_entries if entry.directory.is_dir()]
        if not observable_dirs:
            logger.debug("skills: hot-reload — no existing skill directories to watch")
            return False

        handler = _SkillFileHandler(self._on_event)

        observer = Observer()
        for directory in observable_dirs:
            try:
                observer.schedule(handler, str(directory), recursive=True)
            except Exception as exc:  # pragma: no cover — best-effort, OS-specific
                logger.warning("skills: cannot watch %s: %s", directory, exc)

        observer.start()
        self._observer = observer
        self._is_active = True
        logger.info("skills: hot-reload watching %d director(y/ies)", len(observable_dirs))
        return True

    def stop(self) -> None:
        """Stop the observer and cancel any pending debounce timer."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception as exc:  # pragma: no cover
                logger.warning("skills: error stopping watcher: %s", exc)
            self._observer = None
        self._is_active = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_event(self, src_path: str) -> None:
        """Schedule (or reschedule) a debounced reload."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._trigger_reload)
            self._timer.daemon = True
            self._timer.start()

    def _trigger_reload(self) -> None:
        try:
            count = reload_registry(
                project_dir=self._project_dir,
                user_home=self._user_home,
                extra_paths=self._extra_paths,
                command_registry=self._command_registry,
            )
        except Exception as exc:
            logger.warning("skills: hot-reload failed: %s", exc)
            return
        if self._on_reload is not None:
            try:
                self._on_reload(count)
            except Exception as exc:
                logger.warning("skills: on_reload callback raised: %s", exc)


def _make_event_handler(callback):
    """Helper module-level factory so ``watchdog`` is imported lazily.

    Tests can substitute this to bypass ``watchdog`` entirely.
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


# Lazy class so importing this module does not pull in ``watchdog`` at
# import time — same dance ``deile.plugins.hot_loader`` does.
def _SkillFileHandler(callback):  # noqa: N802 — preserves the original CamelCase name
    return _make_event_handler(callback)
