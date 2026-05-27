"""Per-user preference store backed by ``~/.deile/preferences.json``.

Operations are atomic via tempfile + ``os.replace`` under an exclusive
``fcntl.flock`` on a persistent sentinel file (never renamed). Valid
value types: string, number, boolean, null (no nested objects). Keys
must match ``^[a-z][a-z0-9_.]{0,127}$`` (snake_case, max 128 chars).
Values are capped at 4096 characters.

Issue #340 — full API. Used by:
- ``deile/tools/preference_tools.py`` (function-call tools)
- ``deile/core/context_manager.py`` (#341 — inject prefs into system prompt)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

_KEY_REGEX = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_MAX_VALUE_CHARS = 4096
_VALID_VALUE_TYPES = (str, int, float, bool, type(None))
_PREFS_DIR = Path.home() / ".deile"
_PREFS_FILE = _PREFS_DIR / "preferences.json"
# Persistent lock filename (never renamed). flock on the data file is
# unsafe because ``os.replace`` swaps the inode underneath the lock —
# two writers can end up holding locks on different inodes and both
# race in ``_atomic_write``, both calling ``os.replace`` on the same
# shared tmp path. Using a fixed sentinel file avoids that.
#
# Resolved at lock acquisition time (not module load) so tests that
# patch ``_PREFS_DIR`` get the lock in their tmp dir.
_PREFS_LOCK_NAME = ".preferences.lock"

# ── Validation helpers ──────────────────────────────────────────────────


def _validate_key(key: str) -> None:
    """Raise ``ValueError`` if *key* does not match the allowed pattern."""
    if not isinstance(key, str):
        raise ValueError(f"Preference key must be a string, got {type(key).__name__}")
    if not _KEY_REGEX.match(key):
        raise ValueError(
            f"Invalid preference key: '{key}'. Keys must be snake_case "
            f"(a-z, 0-9, _, .), start with a letter, max 128 chars."
        )


def _validate_value(value: Any) -> None:
    """Raise ``ValueError`` if *value* is not an allowed type / too long."""
    if not isinstance(value, _VALID_VALUE_TYPES):
        raise ValueError(
            f"Preference value type {type(value).__name__} is not allowed. "
            f"Allowed types: string, number, boolean, null."
        )
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        raise ValueError(
            f"Preference value exceeds maximum length of {_MAX_VALUE_CHARS} "
            f"characters (got {len(value)})."
        )


# ── Atomic file operations ──────────────────────────────────────────────


def _ensure_prefs_dir() -> None:
    """Create ``~/.deile/`` if it does not exist (idempotent)."""
    _PREFS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(data: Dict[str, Any]) -> None:
    """Write *data* to a deterministic temp file and atomically rename
    it over ``_PREFS_FILE``.

    The temp path uses a fixed name (``.preferences.tmp``) alongside the
    target file so ``os.replace`` stays atomic. Callers MUST hold
    ``_lock_prefs()`` for the duration of write to prevent two writers
    racing on the shared tmp path.

    The deterministic name avoids ``mkstemp`` which creates a new inode
    per call and can exhaust inode-limited filesystems (e.g. tmpfs in
    containers).
    """
    _ensure_prefs_dir()
    tmp_file = _PREFS_DIR / ".preferences.tmp"
    with open(tmp_file, "w", encoding="utf-8") as tmp_f:
        json.dump(data, tmp_f, indent=2, sort_keys=True, ensure_ascii=False)
        tmp_f.flush()
        os.fsync(tmp_f.fileno())
    os.replace(tmp_file, _PREFS_FILE)


class _lock_prefs:
    """Exclusive lock context manager backed by a persistent sentinel file.

    Locking the data file itself is unsafe — ``os.replace`` swaps its
    inode under the lock, so a writer that opens the data file after a
    rename gets a different inode and acquires the lock with no
    contention against the previous holder. The sentinel file is never
    renamed, so flock on it serializes all writers correctly.

    Falls back to no-op on platforms where ``fcntl.flock`` is unavailable
    (logged once).
    """

    def __init__(self) -> None:
        self._fp = None

    def __enter__(self):
        _ensure_prefs_dir()
        # ``a+`` creates the file if missing; on existing file it does
        # not truncate (lock file content is irrelevant — we only need
        # the inode for flock). Path is resolved from ``_PREFS_DIR`` at
        # acquisition time so tests that patch the dir get the lock in
        # their tmp dir.
        self._fp = open(_PREFS_DIR / _PREFS_LOCK_NAME, "a+")
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX)
        except OSError:
            logger.warning(
                "fcntl.flock not available — preference writes are not "
                "serialized; concurrent writers may corrupt the file."
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fp is None:
            return
        try:
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            self._fp.close()
            self._fp = None


def _read_prefs_file() -> Dict[str, Any]:
    """Read the full preferences.json file, returning an empty dict on
    missing or unparseable file (corruption is recovered silently)."""
    _ensure_prefs_dir()
    try:
        with open(_PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(
                "preferences.json is not a JSON object — recovering with empty dict"
            )
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("preferences.json: %s — returning empty dict", exc)
        return {}


def _write_prefs_file(data: Dict[str, Any]) -> None:
    """Atomically write *data* to preferences.json under the persistent
    lock so concurrent writers never race in ``_atomic_write``."""
    with _lock_prefs():
        _atomic_write(data)


# ── PreferenceStore ─────────────────────────────────────────────────────


class PreferenceStore:
    """Per-user key-value store for DEILE preferences.

    All operations targeting the same user_id are atomic (read-modify-write
    under an exclusive sentinel-file lock). Preference values must be
    string, number, boolean, or null. Keys are snake_case with optional
    dot-namespacing (max 128 chars).
    """

    def store(self, user_id: str, key: str, value: Any) -> None:
        """Persist a single *key* / *value* pair for *user_id*.

        Raises:
            ValueError: if key or value fails validation.
        """
        _validate_key(key)
        _validate_value(value)

        with _lock_prefs():
            data = _read_prefs_file()
            data.setdefault(user_id, {})[key] = value
            _atomic_write(data)

    def get(self, user_id: str, key: str) -> Any:
        """Return the value for *key* under *user_id*, or ``None`` if not
        set."""
        data = _read_prefs_file()
        user_prefs = data.get(user_id, {})
        if not isinstance(user_prefs, dict):
            return None
        return user_prefs.get(key)

    def get_all(self, user_id: str) -> Dict[str, Any]:
        """Return all preferences for *user_id* as a dict."""
        data = _read_prefs_file()
        user_prefs = data.get(user_id, {})
        if not isinstance(user_prefs, dict):
            return {}
        return dict(user_prefs)

    def delete(self, user_id: str, key: str) -> bool:
        """Delete *key* for *user_id*. Returns ``True`` if the key existed,
        ``False`` otherwise. Idempotent — deleting a nonexistent key
        succeeds (returns ``False``)."""
        existed = False
        with _lock_prefs():
            data = _read_prefs_file()
            if user_id in data and isinstance(data[user_id], dict):
                existed = key in data[user_id]
                data[user_id].pop(key, None)
                if not data[user_id]:
                    del data[user_id]
            _atomic_write(data)
        return existed

    def list_keys(self, user_id: str) -> List[str]:
        """Return all key names for *user_id*, sorted alphabetically."""
        data = _read_prefs_file()
        user_prefs = data.get(user_id, {})
        if not isinstance(user_prefs, dict):
            return []
        return sorted(user_prefs.keys())
