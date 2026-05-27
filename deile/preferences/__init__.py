"""User preference persistence for DEILE.

Implements PreferenceStore — a per-user JSON-backed key-value store for
user preferences persisted in ``~/.deile/preferences.json``.

API (Issue #340):
    store(user_id, key, value)
    get(user_id, key) -> value
    get_all(user_id) -> dict
    delete(user_id, key)
    list_keys(user_id) -> list
"""

from .store import PreferenceStore

__all__ = ["PreferenceStore"]
