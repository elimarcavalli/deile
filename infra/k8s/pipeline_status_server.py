#!/usr/bin/env python3
"""Pipeline status HTTP server (issue #347).

Until this module landed the ``deile-pipeline`` Pod had no listening port —
the autonomous loop pulled from the forge and pushed to the worker, but
outside observers had to ``kubectl logs`` to figure out what was happening.

This server is a thin, read-only mirror of the live monitor state, sharing
the same Bearer auth pattern as ``claude_worker_server`` and ``worker_server``
(same secret file location convention, same constant-time comparison).  The
state itself is published by the running monitor into a
:class:`PipelineStatusState` singleton; this module never imports the
monitor (keeping the import graph one-way) and tests can swap the state
freely with their own fixture.

Endpoints (all under Bearer auth except ``/v1/health``):

* ``GET  /v1/health``                       — readiness probe (no auth)
* ``GET  /v1/pipeline-status``              — tick metrics + pod visibility
* ``GET  /v1/pipeline-status/backlog``      — items eligible for next tick
* ``GET  /v1/pipeline-status/recent``       — chronologically-ordered events
* ``GET  /v1/pipeline-status/ledger``       — DispatchLedger snapshot
* ``GET  /v1/pipeline-status/reaper-preview`` — planned reaper actions
* ``POST /v1/pipeline/force-tick``          — request an immediate tick
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from aiohttp import web

logger = logging.getLogger("deile.pipeline_status_server")


# --------------------------------------------------------------------------- #
# State (published by the monitor, read by handlers)
# --------------------------------------------------------------------------- #


class PipelineStatusState:
    """Thread-safe, read-mostly snapshot of pipeline runtime state.

    The publisher (``deile-pipeline``) updates the relevant fields via the
    ``record_*`` helpers; HTTP handlers serialize the latest snapshot.  All
    mutators take an internal lock — they are called rarely (one per tick
    typically) and the consistency guarantee matters more than throughput.

    The state intentionally stores only structured *summaries*, never raw
    logs and never secrets.  Recent events are capped (default 200 entries)
    so memory usage is bounded.
    """

    DEFAULT_RECENT_CAPACITY = 200

    def __init__(self, *, recent_capacity: int = DEFAULT_RECENT_CAPACITY) -> None:
        self._lock = threading.Lock()
        self.started_at: float = time.time()
        self.last_tick_at: Optional[float] = None
        self.next_tick_at: Optional[float] = None
        self.last_tick_duration_seconds: Optional[float] = None
        self.ticks_total: int = 0
        self.errors_total: int = 0
        self.pods_seen: Dict[str, Dict[str, Any]] = {}
        self.schedule_summary: Dict[str, Any] = {}
        self.backlog: List[Dict[str, Any]] = []
        self.reaper_preview: List[Dict[str, Any]] = []
        self.ledger_snapshot: Dict[str, Dict[str, Any]] = {}
        self.recent_events: Deque[Dict[str, Any]] = deque(maxlen=recent_capacity)
        self._force_tick_callback: Optional[Callable[[], None]] = None

    # -- mutator helpers ------------------------------------------------- #

    def record_tick(self, *, now: Optional[float] = None,
                    next_tick_at: Optional[float] = None,
                    duration_seconds: Optional[float] = None,
                    ticks_total: Optional[int] = None,
                    errors_total: Optional[int] = None) -> None:
        """Mark the end of a tick — bumps the counter and timestamps it.

        ``ticks_total``/``errors_total`` let the publisher supply the
        canonical monitor counters in one call (instead of bumping the
        local counter); when omitted the local counter auto-increments.
        ``duration_seconds`` is stored verbatim so the panel can show the
        cost of the most recent tick.
        """
        with self._lock:
            if ticks_total is not None:
                self.ticks_total = int(ticks_total)
            else:
                self.ticks_total += 1
            self.last_tick_at = now if now is not None else time.time()
            if next_tick_at is not None:
                self.next_tick_at = next_tick_at
            if duration_seconds is not None:
                self.last_tick_duration_seconds = float(duration_seconds)
            if errors_total is not None:
                self.errors_total = int(errors_total)

    def record_error(self) -> None:
        with self._lock:
            self.errors_total += 1

    def record_event(self, *, event_type: str, summary: str,
                     ts: Optional[float] = None, **details: Any) -> None:
        """Append a chronological event to ``recent_events``.

        Keep ``summary`` short (under ~120 chars) — the panel renders one
        row per event.  Use ``details`` for structured extras the panel
        can drill into.
        """
        with self._lock:
            event = {
                "ts": ts if ts is not None else time.time(),
                "event_type": event_type,
                "summary": summary[:200],
            }
            if details:
                event["details"] = dict(details)
            self.recent_events.append(event)

    def set_backlog(self, items: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.backlog = list(items)

    def set_reaper_preview(self, items: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.reaper_preview = list(items)

    def set_ledger_snapshot(self, snapshot: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self.ledger_snapshot = dict(snapshot)

    def set_schedule_summary(self, summary: Dict[str, Any]) -> None:
        with self._lock:
            self.schedule_summary = dict(summary)

    def set_pods_seen(self, pods: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self.pods_seen = dict(pods)

    def set_force_tick_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """Register a callback that wakes the monitor loop immediately.

        Typically ``lambda: monitor._force_tick_event.set()``.  ``None``
        disables the endpoint (POST returns ``409``).
        """
        with self._lock:
            self._force_tick_callback = callback

    # -- read helpers ---------------------------------------------------- #

    def snapshot_status(self) -> Dict[str, Any]:
        with self._lock:
            uptime = time.time() - self.started_at
            return {
                "started_at": self.started_at,
                "uptime_seconds": uptime,
                "last_tick_at": self.last_tick_at,
                "next_tick_at": self.next_tick_at,
                "last_tick_duration_seconds": self.last_tick_duration_seconds,
                "ticks_total": self.ticks_total,
                "errors_total": self.errors_total,
                "pods_seen": dict(self.pods_seen),
                "schedule_summary": dict(self.schedule_summary),
                "now": time.time(),
            }

    def snapshot_backlog(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.backlog)

    def snapshot_recent(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self.recent_events)
        # Newest first — the panel renders top-down.
        items.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        if limit is not None:
            items = items[:limit]
        return items

    def snapshot_ledger(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self.ledger_snapshot)

    def snapshot_reaper_preview(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.reaper_preview)

    def trigger_force_tick(self) -> bool:
        """Invoke the registered callback if any; return whether it fired."""
        with self._lock:
            cb = self._force_tick_callback
        if cb is None:
            return False
        try:
            cb()
        except Exception as exc:  # best-effort — surface via logs, not 500
            logger.warning("force-tick callback raised: %s", exc)
            return False
        return True


_global_state: Optional[PipelineStatusState] = None
_global_state_lock = threading.Lock()


def get_global_state() -> PipelineStatusState:
    """Return the process-wide singleton, lazily constructing it."""
    global _global_state
    with _global_state_lock:
        if _global_state is None:
            _global_state = PipelineStatusState()
        return _global_state


def reset_global_state() -> None:
    """Reset the singleton (test-only — never call in production)."""
    global _global_state
    with _global_state_lock:
        _global_state = None


# --------------------------------------------------------------------------- #
# Bearer auth — same convention as the worker servers
# --------------------------------------------------------------------------- #


def _read_auth_token() -> str:
    """Read the Bearer token used to gate access to the status endpoints.

    Same lookup order as ``claude_worker_server._read_auth_token`` to make
    the operator experience uniform: file at the K8s Secret mount, env file
    override, raw env var (test-only).
    """
    candidates = [
        Path("/run/secrets/pipeline-status/PIPELINE_STATUS_BEARER_TOKEN"),
        Path(os.environ.get("DEILE_PIPELINE_STATUS_AUTH_TOKEN_FILE", "")),
    ]
    for p in candidates:
        if p and p.is_file():
            token = p.read_text(encoding="utf-8").strip()
            if token:
                return token
    env_val = os.environ.get("DEILE_PIPELINE_STATUS_AUTH_TOKEN", "").strip()
    if env_val:
        return env_val
    raise RuntimeError(
        "pipeline-status auth token not found: expected "
        "/run/secrets/pipeline-status/PIPELINE_STATUS_BEARER_TOKEN or "
        "DEILE_PIPELINE_STATUS_AUTH_TOKEN env"
    )


@web.middleware
async def _bearer_auth_mw(request: web.Request, handler):
    """Same constant-time Bearer auth used by the other DEILE HTTP services.

    Whitelist ``/v1/health`` for the K8s readinessProbe — every other path
    requires ``Authorization: Bearer <token>`` matching the configured
    token in constant time (``hmac.compare_digest``).
    """
    if request.path == "/v1/health":
        return await handler(request)
    expected = request.app["auth_token"]
    got = request.headers.get("Authorization", "")
    if not got.startswith("Bearer ") or not hmac.compare_digest(
            got[len("Bearer "):], expected):
        return web.json_response(
            {"error": {"code": "UNAUTHORIZED", "message": "bad bearer"}},
            status=401,
        )
    return await handler(request)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def health_handler(request: web.Request) -> web.Response:
    """Readiness/liveness — always 200 once the server is up.

    The pipeline is healthy when this responds — the loop itself may be
    in a 60s poll-sleep, that is normal; ``/v1/pipeline-status`` is where
    the operator inspects activity.
    """
    return web.json_response({"status": "ok"})


async def pipeline_status_handler(request: web.Request) -> web.Response:
    """``GET /v1/pipeline-status`` — tick metrics + pod visibility."""
    state: PipelineStatusState = request.app["status_state"]
    return web.json_response(state.snapshot_status())


async def pipeline_backlog_handler(request: web.Request) -> web.Response:
    """``GET /v1/pipeline-status/backlog`` — items eligible for next tick."""
    state: PipelineStatusState = request.app["status_state"]
    return web.json_response({"backlog": state.snapshot_backlog()})


async def pipeline_recent_handler(request: web.Request) -> web.Response:
    """``GET /v1/pipeline-status/recent`` — chronological events.

    Optional ``limit=<n>`` truncates to the latest N entries.  Capped at
    1000 to keep responses bounded.
    """
    state: PipelineStatusState = request.app["status_state"]
    raw_limit = request.query.get("limit")
    limit: Optional[int] = None
    if raw_limit:
        try:
            limit = max(1, min(int(raw_limit), 1000))
        except ValueError:
            limit = None
    return web.json_response({"events": state.snapshot_recent(limit=limit)})


async def pipeline_ledger_handler(request: web.Request) -> web.Response:
    """``GET /v1/pipeline-status/ledger`` — DispatchLedger snapshot."""
    state: PipelineStatusState = request.app["status_state"]
    return web.json_response({"ledger": state.snapshot_ledger()})


async def pipeline_reaper_preview_handler(request: web.Request) -> web.Response:
    """``GET /v1/pipeline-status/reaper-preview`` — planned reaper actions."""
    state: PipelineStatusState = request.app["status_state"]
    return web.json_response({"actions": state.snapshot_reaper_preview()})


async def pipeline_force_tick_handler(request: web.Request) -> web.Response:
    """``POST /v1/pipeline/force-tick`` — wake the monitor loop immediately.

    Returns ``200 {"triggered": true}`` when the registered callback fires,
    ``409`` when no callback is wired (e.g. monitor hasn't started, or
    running in a test).  No body is required.
    """
    state: PipelineStatusState = request.app["status_state"]
    if state.trigger_force_tick():
        return web.json_response({"triggered": True})
    return web.json_response(
        {"triggered": False, "reason": "no force-tick callback registered"},
        status=409,
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_app(
    *,
    auth_token: Optional[str] = None,
    state: Optional[PipelineStatusState] = None,
) -> web.Application:
    """Build the aiohttp application.

    Args:
        auth_token: Bearer token override (tests pass a fixed value); when
            ``None`` the token is loaded via :func:`_read_auth_token`.
        state: Pipeline state to expose; when ``None`` the global singleton
            is used (so the monitor and the server share the same instance
            naturally).
    """
    app = web.Application(
        middlewares=[_bearer_auth_mw],
        client_max_size=64 * 1024,
    )
    app["auth_token"] = auth_token or _read_auth_token()
    app["status_state"] = state or get_global_state()
    app.router.add_get("/v1/health", health_handler)
    app.router.add_get("/v1/pipeline-status", pipeline_status_handler)
    app.router.add_get("/v1/pipeline-status/backlog", pipeline_backlog_handler)
    app.router.add_get("/v1/pipeline-status/recent", pipeline_recent_handler)
    app.router.add_get("/v1/pipeline-status/ledger", pipeline_ledger_handler)
    app.router.add_get(
        "/v1/pipeline-status/reaper-preview", pipeline_reaper_preview_handler,
    )
    app.router.add_post("/v1/pipeline/force-tick", pipeline_force_tick_handler)
    return app


def main(passthrough: Optional[List[str]] = None) -> int:  # pragma: no cover
    """Entry point used by the deile-pipeline Pod when running standalone."""
    del passthrough
    logging.basicConfig(
        level=os.environ.get("DEILE_PIPELINE_STATUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("DEILE_PIPELINE_STATUS_HOST", "0.0.0.0")
    port = int(os.environ.get("DEILE_PIPELINE_STATUS_PORT", "8768"))
    logger.info("pipeline_status_server listening on %s:%d", host, port)
    app = build_app()
    web.run_app(app, host=host, port=port, print=lambda *_: None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
