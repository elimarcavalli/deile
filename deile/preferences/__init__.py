"""Preference persistence layer for DEILE.

Provides :class:`PreferenceStore` — a per-user key-value store backed by
``~/.deile/preferences.json`` with atomic writes and file locking.
"""

from .store import PreferenceStore

__all__ = ["PreferenceStore"]
