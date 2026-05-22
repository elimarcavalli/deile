"""ISO-8601 UTC datetime helpers shared by pipeline modules.

The scheduler used to define ``_now_utc``/``_parse_dt``/``_serialize_dt``
inline; ``cron/store.py`` carried the same pair under different names
(``_to_iso``/``_from_iso``). Centralising them here keeps the
canonical wire format (``%Y-%m-%dT%H:%M:%SZ``, always UTC-normalised)
in a single place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def format_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Format ``dt`` as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC). ``None`` passes through."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value) -> Optional[datetime]:
    """Parse a datetime/str into a UTC-aware ``datetime``.

    Accepts ``None`` (returns ``None``), a ``datetime`` (returned UTC-aware),
    or a string (ISO-8601, optionally with a single trailing ``Z``). Raises
    ``ValueError`` with a clear prefix (``"invalid ISO datetime: <repr>"``)
    on any unsupported input — callers wrap into their own domain exception
    when needed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"invalid ISO datetime: {value!r}")
        # Trim at most one trailing ``Z`` so a malformed ``...ZZ`` does not
        # silently parse as midnight UTC. Python 3.11+ ``fromisoformat``
        # natively accepts a single trailing ``Z``, so after the strip we
        # must reject any remaining ``Z`` suffix explicitly — otherwise
        # ``...ZZ`` → strip-one → ``...Z`` → fromisoformat → success, and
        # the malformation is silently accepted.
        if stripped.endswith("Z"):
            stripped = stripped[:-1]
            if stripped.endswith("Z"):
                raise ValueError(f"invalid ISO datetime: {value!r}")
        try:
            dt = datetime.fromisoformat(stripped)
        except ValueError as exc:
            raise ValueError(f"invalid ISO datetime: {value!r}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ValueError(f"unsupported datetime value: {value!r}")
