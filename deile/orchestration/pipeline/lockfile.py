"""PID-based lockfile for monitor instance singletons (per host).

Prevents two monitors with the *same* identity running simultaneously on
the same host (which would corrupt the shared `.worktrees/<monitor_id>/`
directory + race on schedule files).

Cross-host coordination is not the goal here — that's covered by hash
sharding (different identities = different shards). This is purely a
local "did I forget to kill the previous one?" guard.
"""

from __future__ import annotations

import errno
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from deile.core.exceptions import DEILEError

logger = logging.getLogger(__name__)


class LockHeldError(DEILEError):
    """Raised when another live process already holds the lock."""

    def __init__(self, path: Path, holder_pid: int) -> None:
        super().__init__(
            f"lock {path} is held by live PID {holder_pid}"
        )
        self.path = path
        self.holder_pid = holder_pid


def _pid_alive(pid: int) -> bool:
    """Return True if the PID corresponds to a running process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def acquire(path: Path, *, holder_pid: Optional[int] = None) -> Path:
    """Try to acquire the lock at ``path``.

    Returns the resolved path on success, raises :class:`LockHeldError` if
    another live PID already holds it. Stale locks (PID dead) are silently
    overwritten.
    """
    pid = holder_pid if holder_pid is not None else os.getpid()
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            existing = int(path.read_text().strip() or "0")
        except (ValueError, OSError):
            existing = 0
        if existing > 0 and existing != pid and _pid_alive(existing):
            raise LockHeldError(path, existing)
        # Stale or self-owned — fall through to overwrite.
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def release(path: Path, *, holder_pid: Optional[int] = None) -> None:
    """Release the lock if owned by ``holder_pid`` (default: current PID).

    Best-effort: missing or foreign-owned locks are left alone, never raise.
    """
    pid = holder_pid if holder_pid is not None else os.getpid()
    path = Path(path).resolve()
    try:
        if not path.exists():
            return
        try:
            current = int(path.read_text().strip() or "0")
        except (ValueError, OSError):
            current = 0
        if current == pid:
            path.unlink()
    except OSError as exc:
        logger.debug("lock release ignored %s: %s", path, exc)


@contextmanager
def pid_lock(path: Path) -> Iterator[Path]:
    """Context manager wrapper around :func:`acquire` / :func:`release`."""
    locked = acquire(path)
    try:
        yield locked
    finally:
        release(locked)
