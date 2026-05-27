"""PreferenceStore — per-user JSON-backed key-value persistence.

Persists user preferences in ``~/.deile/preferences.json`` as a flat JSON
object keyed by ``user_id``, with each value being a dict of ``key: value``.

Atomic writes via tempfile + os.replace; no file-lock dependency.

Issue #340 defines the full API; this module is the minimal implementation
needed for #341 (injection into system prompt). Tools (remember_preference,
list_preferences, forget_preference) are out of scope here — they live in
``deile/tools/preference_tools.py``.

Key regex: ``^[a-z][a-z0-9_.]{0,127}$`` (snake_case with dots, max 128 chars).
Value max: 4096 characters.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_VALUE_MAX_CHARS = 4096
_DEFAULT_PREFS_PATH = Path.home() / ".deile" / "preferences.json"


def _default_prefs_path() -> Path:
    return _DEFAULT_PREFS_PATH


# ---------------------------------------------------------------------------
# PreferenceStore
# ---------------------------------------------------------------------------


class PreferenceStore:
    """Per-user key-value store backed by a single JSON file.

    Thread-safe via tempfile + os.replace (atomic on POSIX).  Not meant for
    high-frequency writes — designed for infrequent preference saves coupled
    with high-frequency reads (system prompt injection every turn).

    Usage::

        store = PreferenceStore()
        store.store("user-123", "response_language", "pt-BR")
        store.get_all("user-123")   # -> {"response_language": "pt-BR"}
    """

    def __init__(self, path: Optional[Union[Path, str]] = None) -> None:
        if path is not None and isinstance(path, str):
            path = Path(path)
        self._path = path or _default_prefs_path()

    # ------------------------------------------------------------------
    # Public API — Issue #340 contract
    # ------------------------------------------------------------------

    def store(self, user_id: str, key: str, value: str) -> None:
        """Persist a preference for *user_id*."""
        self._validate_key(key)
        self._validate_value(value)

        data = self._load()
        user_prefs: Dict[str, Any] = data.get(user_id, {})
        user_prefs[key] = value
        data[user_id] = user_prefs
        self._save(data)

    def get(self, user_id: str, key: str) -> Optional[str]:
        """Retrieve a single preference value, or *None*."""
        data = self._load()
        user_prefs = data.get(user_id, {})
        return user_prefs.get(key)

    def get_all(self, user_id: str) -> Dict[str, Any]:
        """Return all preferences for *user_id* as a dict (empty if none)."""
        data = self._load()
        return dict(data.get(user_id, {}))

    def delete(self, user_id: str, key: str) -> None:
        """Remove a preference.  Idempotent — no error if key doesn't exist."""
        data = self._load()
        user_prefs = data.get(user_id)
        if user_prefs is None:
            return
        user_prefs.pop(key, None)  # idempotent
        if not user_prefs:
            data.pop(user_id, None)  # clean up empty user entry
        else:
            data[user_id] = user_prefs
        self._save(data)

    def list_keys(self, user_id: str) -> List[str]:
        """Return sorted list of keys for *user_id*."""
        data = self._load()
        user_prefs = data.get(user_id, {})
        return sorted(user_prefs.keys())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError(
                f"Invalid preference key: {key!r}. "
                f"Must match {_KEY_RE.pattern} (snake_case with dots, max 128 chars)."
            )

    @staticmethod
    def _validate_value(value: str) -> None:
        if not isinstance(value, str):
            raise ValueError("Preference value must be a string.")
        if len(value) > _VALUE_MAX_CHARS:
            raise ValueError(
                f"Preference value exceeds {_VALUE_MAX_CHARS} characters "
                f"(got {len(value)})."
            )

    # ------------------------------------------------------------------
    # Internal — JSON I/O
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Load the full preferences dict, or return empty dict on any error."""
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                logger.warning("preferences.json is not a dict — resetting")
                return {}
            return data
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load preferences from %s: %s", self._path, exc)
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        """Atomically write *data* to the preferences file.

        Uses tempfile + os.replace for atomicity — no file locks needed.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=".preferences-",
                suffix=".tmp",
            )
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.error("Failed to write preferences to %s: %s", self._path, exc)
            raise
