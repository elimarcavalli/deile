"""File-system watcher that reloads the unified ``SkillRegistry`` in place.

Concurrency:
- ``watchdog`` runs the observer on its own thread.
- Editor write-bursts are debounced via a ``threading.Timer`` guarded by
  ``_timer_lock`` (many editors emit multiple writes per save).
- ``reload_registry`` swaps registry contents atomically via
  ``SkillRegistry.replace_all`` so readers never see a torn state.
- ``_RELOAD_LOCK`` serializes overlapping reloads (manual + watcher; or two
  watcher events while a rescan is still running).
- ``stop()`` is idempotent and joins the observer thread before returning.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from .discovery import default_scan_order, discover_skills_sync
from .registry import get_skill_registry
from .slash_command_bridge import (register_skills_as_commands,
                                   unregister_skill_commands)

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
        observable_dirs = [entry.directory for entry in scan_entries if entry.directory.is_dir()]
        if not observable_dirs:
            missing = [entry.directory for entry in scan_entries]
            logger.warning(
                "skills: hot-reload not started — none of the %d configured "
                "skill directories exists yet: %s",
                len(scan_entries),
                ", ".join(str(p) for p in missing),
            )
            return False

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
                    directory, exc,
                )

        if scheduled_count == 0:
            logger.warning("skills: hot-reload not started — every observer.schedule() call failed")
            return False

        observer.start()
        self._observer = observer
        self._is_active = True
        logger.info("skills: hot-reload watching %d directory/ies", scheduled_count)
        return True

    def stop(self) -> None:
        """Idempotent — cancels any pending debounce timer and joins the observer."""
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

    def _on_event(self, src_path: str) -> None:
        if self._stopping:
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._trigger_reload)
            self._timer.daemon = True
            self._timer.start()

    def _trigger_reload(self) -> None:
        # Clear the timer reference first so a fresh event arriving while we
        # run can start a new timer without races.
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
                type(exc).__name__, exc,
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
