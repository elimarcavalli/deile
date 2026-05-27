"""User preference persistence for DEILE.

Provides :class:`PreferenceStore` — a per-user key-value store backed by
``~/.deile/preferences.json`` with atomic writes and file locking.

API (Issue #340):
    store(user_id, key, value)
    get(user_id, key) -> value
    get_all(user_id) -> dict
    delete(user_id, key) -> bool
    list_keys(user_id) -> list
"""

from .store import PreferenceStore

__all__ = ["PreferenceStore"]
