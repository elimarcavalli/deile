"""Per-user preference store backed by ``~/.deile/preferences.json``.

Operations are atomic using tempfile + atomic rename. Valid value types:
string, number, boolean, null (no nested objects). Keys must match
``^[a-z][a-z0-9_.]{0,127}$`` (snake_case, max 128 chars). Values are
capped at 4096 characters.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

_KEY_REGEX = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_MAX_VALUE_CHARS = 4096
_VALID_VALUE_TYPES = (str, int, float, bool, type(None))
_PREFS_DIR = Path.home() / ".deile"
_PREFS_FILE = _PREFS_DIR / "preferences.json"

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

    The temp path is derived from the current ``_PREFS_FILE`` (so tests
    that patch ``_PREFS_FILE`` / ``_PREFS_DIR`` get a temp file on the
    same filesystem). Uses a fixed name (``.preferences.tmp``) alongside
    the target file so ``os.replace`` stays atomic. Because all
    read-modify-write paths that call this already hold ``fcntl.flock``
    on the main file, there is no risk of concurrent writers trampling
    each other's temp file.

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
    """Atomically write *data* to preferences.json.

    Acquires an exclusive file lock before writing so concurrent readers
    never see a partial file."""
    _ensure_prefs_dir()
    with open(_PREFS_FILE, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError:
            logger.warning(
                "fcntl.flock not available — falling back to rename-only atomicity"
            )
        _atomic_write(data)


# ── PreferenceStore ─────────────────────────────────────────────────────


class PreferenceStore:
    """Per-user key-value store for DEILE preferences.

    All operations targeting the same user_id are atomic (read-modify-write
    under a file lock). Preference values must be string, number, boolean,
    or null. Keys are snake_case with optional dot-namespacing (max 128
    chars).
    """

    def store(self, user_id: str, key: str, value: Any) -> None:
        """Persist a single *key* / *value* pair for *user_id*.

        Raises:
            ValueError: if key or value fails validation.
        """
        _validate_key(key)
        _validate_value(value)

        _ensure_prefs_dir()
        with open(_PREFS_FILE, "a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass

            # Read from the actual file path (not the lock fd) so we
            # always see the latest data even after another thread's
            # os.replace switched the inode under us.
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
        _ensure_prefs_dir()
        existed = False
        with open(_PREFS_FILE, "a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass

            # Read from the actual file path (not the lock fd) so we
            # always see the latest data even after another thread's
            # os.replace switched the inode under us.
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
