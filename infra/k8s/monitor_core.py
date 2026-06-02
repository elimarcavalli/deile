"""Deterministic engine for the DEILE-Monitor tick (Phase A).

Reusable primitives shared by the vigias and the tick orchestrator. NO LLM here:
this module is pure stdlib + injected subprocess runners, so every unit is unit
testable. The structured-emit format and anti-flood semantics are a hard contract
with ``deile/personas/instructions/monitor.md`` (schema test
``deile/tests/personas/test_monitor_emit_schema.py``) and the panel parser
``infra/k8s/_panel_monitor.py`` — see the spec in
``docs/superpowers/specs/2026-06-02-monitor-deterministic-tick.md``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants (contract with monitor.md / panel)
# ---------------------------------------------------------------------------

EMIT_MAX_LEN = 500
HOURLY_NOTIFY_CAP = 8
SEVERITY_COOLDOWN_S: Dict[str, int] = {"P0": 900, "P1": 7200, "P2": 14400}
SEVERITY_EMOJI: Dict[str, str] = {"P0": "🔴", "P1": "🟡", "P2": "🔵"}

# DNS-first kube-api endpoints (matches monitor.md _resolve_kube_api order).
_KUBE_API_ENDPOINTS = (
    "https://kubernetes.default.svc:443",
    "https://kubernetes.default.svc.cluster.local:443",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """ISO-8601 UTC with trailing Z (matches the audit-log timestamp format)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def sanitize_emit_line(line: str) -> str:
    """Truncate to 500 chars then strip control chars (matches bash ``_emit``).

    Order matters: the bash helper applies ``${line:0:500}`` BEFORE the
    newline/cr/tab substitution, so we replicate truncate-then-strip.
    """
    line = line[:EMIT_MAX_LEN]
    for ch in ("\n", "\r", "\t"):
        line = line.replace(ch, " ")
    return line


@dataclass
class TickFlags:
    """Per-tick cardinality guards + counters (fresh per tick / per process)."""
    pvc_fail_emitted: bool = False
    flood_cap_notify_emitted: bool = False
    flood_cap_fu_emitted: bool = False
    actions: int = 0
    notifications: int = 0


class Emitter:
    """Structured-event emitter: stdout-first, then PVC audit-log append.

    On audit append failure (PVC degraded), emits ``monitor.audit_pvc_fail`` to
    stdout exactly once per tick (guarded by ``flags.pvc_fail_emitted``), so the
    live stream (kubectl logs) stays the source of truth.
    """

    def __init__(
        self,
        audit_path: str,
        flags: TickFlags,
        *,
        out=None,
        clock: Optional[Callable[[], datetime]] = None,
        tick_n: Any = "?",
    ) -> None:
        self.audit_path = audit_path
        self.flags = flags
        self.out = out if out is not None else sys.stdout
        self.clock = clock or utcnow
        self.tick_n = tick_n

    def emit(self, line: str) -> None:
        line = sanitize_emit_line(line)
        print(line, file=self.out)
        ts = iso(self.clock())
        try:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(f"{ts} {line}\n")
        except OSError as exc:
            if not self.flags.pvc_fail_emitted:
                print(
                    f"monitor.audit_pvc_fail reason='write failed' "
                    f"errno={exc.errno} tick=#{self.tick_n}",
                    file=self.out,
                )
                self.flags.pvc_fail_emitted = True


# ---------------------------------------------------------------------------
# Anti-flood predicates
# ---------------------------------------------------------------------------

def cooldown_seconds(severity: str) -> int:
    return SEVERITY_COOLDOWN_S.get(severity, SEVERITY_COOLDOWN_S["P2"])


def is_acked(anomaly: Dict[str, Any], now: datetime) -> bool:
    acked = _parse_iso(anomaly.get("acked_until"))
    return acked is not None and now < acked


def in_cooldown(anomaly: Dict[str, Any], severity: str, now: datetime) -> bool:
    last = _parse_iso(anomaly.get("last_notified"))
    if last is None:
        return False
    return (now - last).total_seconds() < cooldown_seconds(severity)


def _in_cooldown_override(
    anomaly: Dict[str, Any], severity: str, now: datetime, min_interval_s: Optional[int]
) -> bool:
    """Cooldown check using ``min_interval_s`` when given, else the severity default.

    A vigia may demand a longer-than-severity renotify window (e.g. orphan issues
    renotify every 6h even though P1 defaults to 2h).
    """
    last = _parse_iso(anomaly.get("last_notified"))
    if last is None:
        return False
    window = min_interval_s if min_interval_s is not None else cooldown_seconds(severity)
    return (now - last).total_seconds() < window


def hour_slot(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00")


def hourly_cap_reached(state: Dict[str, Any], now: datetime, cap: int = HOURLY_NOTIFY_CAP) -> bool:
    """True if the hourly notify cap is reached *for the current hour*.

    A change of UTC hour logically resets the counter (the caller resets the
    persisted counter on emit); this predicate is read-only.
    """
    if state.get("hour_slot") != hour_slot(now):
        return False
    return int(state.get("notifications_this_hour", 0)) >= cap


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def default_state() -> Dict[str, Any]:
    return {
        "last_tick": 0,
        "last_tick_epoch": 0,
        "known_anomalies": {},
        "notifications_this_hour": 0,
        "hour_slot": "",
        "paused_until": None,
        "fu_fingerprints": [],
        "fu_created_today": 0,
        "fu_day_slot": "",
    }


def load_state(path: str) -> Dict[str, Any]:
    """Read the state JSON, returning defaults (merged) when missing or corrupt."""
    base = default_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            base.update(data)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return base


def save_state(path: str, state: Dict[str, Any]) -> None:
    """Atomic write (tmp in same dir + os.replace)."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".monitor-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_anomaly(
    state: Dict[str, Any],
    fingerprint: str,
    *,
    severity: str,
    atype: str,
    now: datetime,
    **extra: Any,
) -> Dict[str, Any]:
    """Upsert a known-anomaly entry, preserving ``first_seen`` and bumping ``count``."""
    known = state.setdefault("known_anomalies", {})
    entry = known.get(fingerprint)
    if entry is None:
        entry = {"first_seen": iso(now), "count": 0}
        known[fingerprint] = entry
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last_seen"] = iso(now)
    entry["severity"] = severity
    entry["type"] = atype
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Notification composition
# ---------------------------------------------------------------------------

_NOTIFY_FOOTER = (
    "\n\nComandos rápidos (celular):\n"
    "  /status — visão geral do cluster\n"
    "  /monitor pause 30m — pausa o monitor por 30min"
)


def compose_notification(severity: str, title: str, body: str) -> str:
    """Render the canonical notification message (emoji + header + body + footer)."""
    emoji = SEVERITY_EMOJI.get(severity, "🔵")
    return f"{emoji} [DEILE-MONITOR] {severity}: {title}\n\n{body}{_NOTIFY_FOOTER}"


# ---------------------------------------------------------------------------
# Command runner (graceful — never raises; principle P4)
# ---------------------------------------------------------------------------

@dataclass
class CmdResult:
    rc: int
    out: str
    err: str


def run_cmd(args: List[str], *, timeout: int = 15, input_text: Optional[str] = None) -> CmdResult:
    """Run a subprocess, returning a :class:`CmdResult`. Never raises.

    A missing binary, timeout or any OS error is folded into a non-zero rc so a
    single failing vigia can never crash the tick (graceful-in-absence principle).
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
        return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return CmdResult(124, exc.stdout or "" if isinstance(exc.stdout, str) else "", "timeout")
    except (OSError, ValueError) as exc:
        return CmdResult(127, "", str(exc))


# ---------------------------------------------------------------------------
# Notifier — anti-flood gate + delivery + monitor.notify emit
# ---------------------------------------------------------------------------

class Notifier:
    """Gate a notification through ack/cooldown/hourly-cap, deliver it (DM via
    deilebot or log-only fallback), update state and emit ``monitor.notify``.

    ``notify`` returns True when a notification was emitted (sent or log-only),
    False when suppressed (acked / in cooldown / hourly cap). The vigia must call
    :func:`record_anomaly` for the fingerprint *before* calling ``notify`` so the
    gate can read its prior ``last_notified``.
    """

    def __init__(
        self,
        *,
        state: Dict[str, Any],
        emitter: "Emitter",
        flags: TickFlags,
        run: Callable[..., CmdResult],
        bot_endpoint: str,
        bot_token: str,
        user_id: str,
        notif_log_path: str,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.state = state
        self.emitter = emitter
        self.flags = flags
        self.run = run
        self.bot_endpoint = bot_endpoint.rstrip("/")
        self.bot_token = bot_token
        self.user_id = user_id
        self.notif_log_path = notif_log_path
        self.clock = clock or utcnow

    def notify(
        self,
        fingerprint: str,
        severity: str,
        title: str,
        body: str,
        *,
        min_interval_s: Optional[int] = None,
    ) -> bool:
        now = self.clock()
        anomaly = self.state.setdefault("known_anomalies", {}).get(fingerprint, {})

        if is_acked(anomaly, now):
            return False
        if _in_cooldown_override(anomaly, severity, now, min_interval_s):
            return False
        if hourly_cap_reached(self.state, now):
            if not self.flags.flood_cap_notify_emitted:
                self.emitter.emit(
                    f"monitor.flood_cap kind=notify reason='hourly cap reached' "
                    f"count={self.state.get('notifications_this_hour', 0)} "
                    f"cap={HOURLY_NOTIFY_CAP} window=1h"
                )
                self.flags.flood_cap_notify_emitted = True
            return False

        message = compose_notification(severity, title, body)
        channel, ok = self._deliver(message, fingerprint)

        # Reset the hourly counter when the UTC hour rolled over, then increment.
        slot = hour_slot(now)
        if self.state.get("hour_slot") != slot:
            self.state["hour_slot"] = slot
            self.state["notifications_this_hour"] = 0
        self.state["notifications_this_hour"] = int(self.state.get("notifications_this_hour", 0)) + 1

        entry = self.state["known_anomalies"].setdefault(fingerprint, {})
        entry["last_notified"] = iso(now)

        msg_head = sanitize_emit_line(message)[:80].replace("'", " ")
        self.emitter.emit(
            f"monitor.notify fingerprint={fingerprint} severity={severity} "
            f"channel={channel} ok={'true' if ok else 'false'} msg_head='{msg_head}'"
        )
        self.flags.notifications += 1
        return True

    def _deliver(self, message: str, fingerprint: str) -> tuple:
        """Return ``(channel, ok)``. DM via deilebot when a user-id is set,
        else log-only. A failed curl falls back to log-only."""
        if self.user_id:
            payload = json.dumps({"user_id": self.user_id, "message": message})
            res = self.run(
                [
                    "curl", "-s", "-S", "--max-time", "10",
                    "-X", "POST", f"{self.bot_endpoint}/v1/notify",
                    "-H", f"Authorization: Bearer {self.bot_token}",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                ],
                timeout=15,
            )
            if res.rc == 0:
                return "dm", True
        # Fallback (no user-id OR curl failed): append to the notifications log.
        ok = self._log_only(message, fingerprint)
        return "log-only", ok

    def _log_only(self, message: str, fingerprint: str) -> bool:
        line = sanitize_emit_line(message)[:100]
        try:
            with open(self.notif_log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{iso(self.clock())} NOTIFY {fingerprint} {line}\n")
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Kube-API DNS-first resolver
# ---------------------------------------------------------------------------

def resolve_kube_api(runner: Callable[[str], int]) -> Optional[str]:
    """Return the first reachable kube-api endpoint, or None if all fail.

    ``runner(endpoint)`` returns a process return code (0 = reachable). The
    Service-IP fallback is appended from the in-pod env vars.
    """
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    endpoints = (*_KUBE_API_ENDPOINTS, f"https://{host}:{port}")
    for endpoint in endpoints:
        try:
            if runner(endpoint) == 0:
                return endpoint
        except Exception:
            continue
    return None
