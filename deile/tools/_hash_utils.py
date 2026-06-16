"""Shared hashing helpers for tool audit logs.

Tools must never log raw user content (Discord text, WhatsApp recipients,
image bytes). They log a stable short hash so two records of the same
content correlate without leaking the content itself.
"""

from __future__ import annotations

import hashlib
from typing import Union


def sha8(payload: Union[str, bytes]) -> str:
    """Return the first 8 hex chars of SHA-256(payload).

    Strings are encoded as UTF-8 with ``errors='replace'`` so adversarial
    surrogate halves can't crash the hash path before audit.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()[:8]
