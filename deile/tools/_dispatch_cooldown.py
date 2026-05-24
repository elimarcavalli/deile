"""Shared helpers for the dispatch tools' anti-loop cooldown registry.

Both :class:`deile.tools.dispatch_deile_task.DispatchDeileTaskTool` and
:class:`deile.tools.dispatch_parallel_subagents.DispatchParallelSubagentsTool`
maintain a recent-key → monotonic-timestamp store and re-implement the
same prune/check/record pattern. These helpers centralize that logic
without altering the tools' class-level API (``_LAST_DISPATCH``,
``_DISPATCH_COOLDOWN_S``) — the store dict is passed by reference, so the
class attribute remains the single source of truth that tests
introspect and clear.

The helpers are deliberately functions (no classes / ABCs): each tool
already owns its store and cooldown constants, the duplication being
removed is just the three operations below.

Lock-prune logic (e.g. ``_CHANNEL_LOCKS`` orphan eviction in
``DispatchDeileTaskTool``) is intentionally NOT covered here — it is
tool-specific and trivially inlined.
"""

from __future__ import annotations

import time
from typing import Dict, Optional


def prune_expired(
    store: Dict[str, float],
    cooldown_s: float,
    now: float,
    max_entries: Optional[int] = None,
) -> None:
    """Drop entries in ``store`` whose timestamp is older than ``cooldown_s``.

    ``cooldown_s`` is the cutoff age in seconds: any entry with
    ``now - ts > cooldown_s`` is removed. Callers that want to retain
    entries for several cooldown periods (typical "keep around for
    debugging" pattern) pass ``cooldown_s = base_cooldown * factor``.

    If ``max_entries`` is given and the store still exceeds it after
    pruning expired entries, the oldest remaining entries are dropped
    until the size is within the cap.
    """
    stale = [k for k, ts in store.items() if (now - ts) > cooldown_s]
    for k in stale:
        store.pop(k, None)
    if max_entries is not None and len(store) > max_entries:
        # Sort by timestamp ascending (oldest first) and drop the excess.
        excess = len(store) - max_entries
        oldest = sorted(store.items(), key=lambda kv: kv[1])[:excess]
        for k, _ in oldest:
            store.pop(k, None)


def is_in_cooldown(
    store: Dict[str, float],
    key: str,
    cooldown_s: float,
    now: Optional[float] = None,
) -> bool:
    """Return ``True`` iff ``store[key]`` was set within the last ``cooldown_s``.

    Missing keys (no prior dispatch) return ``False``. ``now`` defaults
    to ``time.monotonic()`` so callers that don't need to share a
    timestamp across multiple checks can omit it.
    """
    if now is None:
        now = time.monotonic()
    last = store.get(key)
    if last is None:
        return False
    return (now - last) < cooldown_s


def record_dispatch(
    store: Dict[str, float],
    key: str,
    now: Optional[float] = None,
) -> None:
    """Write ``store[key] = now`` (defaults to ``time.monotonic()``)."""
    if now is None:
        now = time.monotonic()
    store[key] = now
