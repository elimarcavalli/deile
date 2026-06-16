"""Canonical structured-log helper for the deile-pipeline pod.

Emits 14 event families to the ``deile.pipeline.events`` logger in the format::

    familia.subtipo  k1=v1 k2='v com espaço' k3=42 ...

Every public function isolates failures: no exception propagates to call-sites.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Hashable

_LOG = logging.getLogger("deile.pipeline.events")

_DEDUP_MAX_KEYS = 2048
_TRUNCATE_FIELD = 200
_MAX_LINE = 500


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize(value: str) -> str:
    """Strip control chars and replace internal single-quotes with space."""
    for ch in ("\n", "\r", "\t"):
        value = value.replace(ch, "")
    value = value.replace("'", " ")
    return value


def _truncate(value: str, limit: int = _TRUNCATE_FIELD) -> str:
    if len(value) > limit:
        return value[:limit] + "..."
    return value


def _fmt_value(v: object) -> str:
    """Format a single scalar value for a k=v pair."""
    if isinstance(v, list):
        inner = ",".join(str(e) for e in v)
        return f"[{inner}]"
    if isinstance(v, str):
        v = _sanitize(v)
        if " " in v:
            return f"'{v}'"
        return v
    return str(v)


def _build_line(family: str, **fields: object) -> str:
    """Build the canonical log line; truncates to _MAX_LINE chars."""
    parts = []
    for k, v in fields.items():
        fv = _fmt_value(v)
        parts.append(f"{k}={fv}")
    line = f"{family}  " + " ".join(parts)
    if len(line) > _MAX_LINE:
        line = line[:_MAX_LINE]
    return line


# ---------------------------------------------------------------------------
# _DedupCache
# ---------------------------------------------------------------------------


class _DedupCache:
    """TTL-based dedup cache with bounded size (eviction at _DEDUP_MAX_KEYS)."""

    def __init__(self) -> None:
        self._seen: dict[Hashable, float] = {}
        self._lock = threading.Lock()

    def seen_recently(self, key: Hashable, ttl: float) -> bool:
        """Return True if *key* was recorded within *ttl* seconds; record if not."""
        now = time.monotonic()
        with self._lock:
            ts = self._seen.get(key)
            if ts is not None and now - ts < ttl:
                return True
            # Record / refresh
            self._seen[key] = now
            # Eviction: purge expired first, then oldest if still over cap
            if len(self._seen) > _DEDUP_MAX_KEYS:
                expired = [k for k, t in self._seen.items() if now - t >= ttl]
                for k in expired:
                    del self._seen[k]
            if len(self._seen) > _DEDUP_MAX_KEYS:
                # Remove oldest entries until within cap
                sorted_keys = sorted(self._seen, key=lambda k: self._seen[k])
                for k in sorted_keys:
                    if len(self._seen) <= _DEDUP_MAX_KEYS:
                        break
                    del self._seen[k]
            return False

    def __len__(self) -> int:
        return len(self._seen)


# Module-level dedup cache (singleton; state lost on restart — acceptable)
_DEDUP = _DedupCache()


# ---------------------------------------------------------------------------
# Public API — 15 typed functions
# ---------------------------------------------------------------------------


def log_refinement_critique(
    *, issue: int, round: int, persona: str, verdict: str, gaps: str = ""
) -> None:
    try:
        kw: dict[str, object] = dict(
            issue=issue, round=round, persona=persona, verdict=verdict
        )
        if gaps:
            kw["gaps"] = _truncate(_sanitize(gaps))
        line = _build_line("refinement.critique", **kw)
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: refinement.critique emit failed")
        except Exception:
            pass


def log_refinement_refine(
    *, issue: int, round: int, persona: str, body_chars: int, verdict: str
) -> None:
    try:
        line = _build_line(
            "refinement.refine",
            issue=issue,
            round=round,
            persona=persona,
            body_chars=body_chars,
            verdict=verdict,
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: refinement.refine emit failed")
        except Exception:
            pass


def log_decomposition_fanout(
    *, intent: int, derivadas: list[int], complexity: list[str]
) -> None:
    try:
        line = _build_line(
            "decomposition.fanout",
            intent=intent,
            derivadas=derivadas,
            complexity=complexity,
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: decomposition.fanout emit failed")
        except Exception:
            pass


def log_batch_claim(*, sha: str, issues: list[int], reason: str) -> None:
    try:
        line = _build_line(
            "batch.claim",
            sha=sha,
            issues=issues,
            reason=_truncate(_sanitize(reason)),
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: batch.claim emit failed")
        except Exception:
            pass


def log_batch_release(*, sha: str, reason: str) -> None:
    try:
        line = _build_line(
            "batch.release",
            sha=sha,
            reason=_truncate(_sanitize(reason)),
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: batch.release emit failed")
        except Exception:
            pass


def log_label_change(
    *, target_kind: str, target: int, removed: list[str], added: list[str]
) -> None:
    try:
        key: Hashable = (target_kind, target, frozenset(removed), frozenset(added))
        if _DEDUP.seen_recently(key, ttl=30.0):
            return
        line = _build_line(
            "label.change",
            target_kind=target_kind,
            target=target,
            removed=removed,
            added=added,
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: label.change emit failed")
        except Exception:
            pass


def log_reaper_unblock(
    *,
    target_kind: str,
    target: int,
    attempts: int,
    reason: str,
    last_activity_s: int | None = None,
) -> None:
    try:
        key: Hashable = (target_kind, target, attempts)
        if _DEDUP.seen_recently(key, ttl=60.0):
            return
        kw: dict[str, object] = dict(
            target_kind=target_kind,
            target=target,
            attempts=attempts,
            reason=_truncate(_sanitize(reason)),
        )
        if last_activity_s is not None:
            kw["last_activity_s"] = last_activity_s
        line = _build_line("reaper.unblock", **kw)
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: reaper.unblock emit failed")
        except Exception:
            pass


def log_reaper_block(
    *, target_kind: str, target: int, attempts: int, cap: int, reason: str
) -> None:
    try:
        key: Hashable = (target_kind, target, attempts)
        if _DEDUP.seen_recently(key, ttl=60.0):
            return
        line = _build_line(
            "reaper.block",
            target_kind=target_kind,
            target=target,
            attempts=attempts,
            cap=cap,
            reason=_truncate(_sanitize(reason)),
        )
        _LOG.warning(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: reaper.block emit failed")
        except Exception:
            pass


def log_auth_fail(*, target: str, attempts: int, threshold: int, reason: str) -> None:
    try:
        key: Hashable = ("auth.fail", target)
        if _DEDUP.seen_recently(key, ttl=60.0):
            return
        line = _build_line(
            "auth.fail",
            target=target,
            attempts=attempts,
            threshold=threshold,
            reason=_truncate(_sanitize(reason)),
        )
        _LOG.warning(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: auth.fail emit failed")
        except Exception:
            pass


def log_auth_backoff(
    *, target: str, attempts: int, until_iso: str, backoff_s: int
) -> None:
    try:
        line = _build_line(
            "auth.backoff",
            target=target,
            attempts=attempts,
            until_iso=until_iso,
            backoff_s=backoff_s,
        )
        _LOG.warning(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: auth.backoff emit failed")
        except Exception:
            pass


def log_auth_skip(*, target: str, until_iso: str, remaining_s: int) -> None:
    try:
        line = _build_line(
            "auth.skip",
            target=target,
            until_iso=until_iso,
            remaining_s=remaining_s,
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: auth.skip emit failed")
        except Exception:
            pass


def log_auth_recover(*, target: str, reason: str) -> None:
    try:
        line = _build_line(
            "auth.recover",
            target=target,
            reason=_truncate(_sanitize(reason)),
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: auth.recover emit failed")
        except Exception:
            pass


def log_routing_mention(*, target_kind: str, target: int, action: str) -> None:
    try:
        line = _build_line(
            "routing.mention",
            target_kind=target_kind,
            target=target,
            action=action,
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: routing.mention emit failed")
        except Exception:
            pass


def log_routing_pr_unified(*, target: int, role: str, mode: str) -> None:
    try:
        line = _build_line(
            "routing.pr_unified",
            target=target,
            role=role,
            mode=mode,
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: routing.pr_unified emit failed")
        except Exception:
            pass


def log_routing_dropped(*, target_kind: str, target: int, reason: str) -> None:
    try:
        line = _build_line(
            "routing.dropped",
            target_kind=target_kind,
            target=target,
            reason=_truncate(_sanitize(reason)),
        )
        _LOG.info(line)
    except Exception:
        try:
            _LOG.debug("pipeline_logger: routing.dropped emit failed")
        except Exception:
            pass
