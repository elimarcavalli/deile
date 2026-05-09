"""Defensive guard against infinite tool-call loops.

The agent's tool-loop is bounded by a hard iteration cap (currently 25) and
the LLM's own decision to stop calling tools. In practice, that is not
enough: a confused model — typically after a tool returns an error or empty
data — can spin on the same call with the same arguments until the cap is
hit, burning the user's tokens and producing no useful answer.

``ToolLoopGuard`` is a small stateful detector that lives next to every
tool-loop driver (``ToolLoopExecutor`` for streaming, the per-provider
``chat_with_tools`` methods for non-streaming). The driver feeds each call
into the guard before executing it; the guard returns either ``None`` (safe
to proceed) or an :class:`AbortReason` describing why the loop must break.
On abort, the guard:

* logs a structured WARNING with the loop signature, and
* emits ``AuditEventType.SUSPICIOUS_ACTIVITY`` so the security audit log
  shows the abort.

Detection rules (any one trips the guard):

1. **Hard ceiling on calls per turn** — independent of iterations, since a
   single iteration can issue several parallel tool calls.
2. **Identical-call repetition** — ≥3 consecutive ``(tool_name, args)``
   tuples with the same hash.
3. **Sliding window** — within any window of 5 consecutive calls, ≥3
   share the same hash (catches the "A, A, B, A" pattern where one
   different call was sandwiched in).
4. **No-progress** — ≥``no_progress_threshold`` consecutive calls returning
   empty/error results while the model keeps calling. The driver reports
   each result back via :meth:`record_result`.

The guard is intentionally framework-agnostic: it knows nothing about
streams, providers, or tools — it just consumes ``(name, args)`` and
``ToolResult`` shapes. That keeps it cheap to wire into every loop site.

Configuration via ``~/.deile/settings.json`` (``loop_guard.*`` section).
DEILE_LOOP_GUARD_* env vars still work as deprecated fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)


class AbortKind(Enum):
    """Why the guard tripped — used to format the user-facing message."""

    MAX_CALLS = "max_calls_exceeded"
    IDENTICAL_REPEAT = "identical_call_repeated"
    SLIDING_WINDOW = "sliding_window_repeats"
    NO_PROGRESS = "no_progress"


@dataclass
class AbortReason:
    """Structured payload returned when the guard trips."""

    kind: AbortKind
    tool_name: str
    args_hash: str
    args_preview: str
    repeat_count: int
    total_calls: int
    detail: str

    def user_message(self) -> str:
        """One-paragraph user-visible explanation of the loop break.

        For path-related tool loops we additionally point at ``bash_execute``
        — observed real failure mode: the model loops on ``list_files`` with
        a path that the sandbox keeps rejecting, when the user actually
        wanted a parent-repo / system-absolute path that only ``bash_execute``
        can reach.
        """
        suffix = _bash_failover_hint(self.tool_name, self.args_preview)
        if self.kind is AbortKind.MAX_CALLS:
            return (
                "I stopped because I exceeded the maximum number of tool calls "
                f"allowed for a single turn ({self.total_calls}). The last call "
                f"was {self.tool_name}({self.args_preview}). Please rephrase your "
                "request or break it into smaller steps." + suffix
            )
        if self.kind is AbortKind.IDENTICAL_REPEAT:
            return (
                f"I detected that I was about to call '{self.tool_name}' with the "
                f"same arguments {self.repeat_count} times in a row "
                f"({self.args_preview}). That is almost certainly a loop, so I "
                "stopped before wasting more tokens. Please try a different phrasing "
                "or be more specific about what you want." + suffix
            )
        if self.kind is AbortKind.SLIDING_WINDOW:
            return (
                f"I detected that '{self.tool_name}' had been called {self.repeat_count} "
                "times with the same arguments in a small window of recent calls "
                f"({self.args_preview}). That looks like a loop, so I stopped. "
                "Please rephrase your request." + suffix
            )
        # NO_PROGRESS
        return (
            f"I made {self.repeat_count} consecutive tool calls without producing "
            "any useful result (the calls returned empty or errored). Rather than "
            "keep retrying, I stopped. Please check the most recent tool errors "
            "and try a different approach." + suffix
        )


# Tools whose arguments include a ``path``/``file_path``/``directory`` field
# and whose loops typically come from a sandbox-rejected path the user actually
# wanted resolved system-wide. When one of these loops, hint at bash_execute.
_PATH_TOOL_NAMES = frozenset({
    "list_files", "read_file", "write_file", "delete_file", "edit_file",
})

# Substrings in args_preview that indicate the LLM was reaching for a path
# outside the working_directory — leading slash (system-absolute), parent
# traversal, or home shorthand.
_OUTSIDE_PROJECT_MARKERS = ('"/', "'/", '"..', "'..", '"~', "'~")


def _bash_failover_hint(tool_name: str, args_preview: str) -> str:
    """Return a one-line addendum suggesting ``bash_execute`` when the
    looping call looks like a sandboxed path-tool reaching outside CWD.

    Pure string check — never raises, returns ``""`` when no hint applies.
    """
    if tool_name not in _PATH_TOOL_NAMES:
        return ""
    if not args_preview or not any(m in args_preview for m in _OUTSIDE_PROJECT_MARKERS):
        return ""
    return (
        " HINT: the path looks like it targets OUTSIDE the project working "
        "directory. file_tools cannot escape the sandbox — use `bash_execute` "
        "with the absolute path instead (e.g. `bash_execute(command=\"ls "
        "<abs_path>\")` or `bash_execute(command=\"cat <abs_path>\")`)."
    )


@dataclass
class _CallRecord:
    tool_name: str
    args_hash: str


@dataclass
class ToolLoopGuard:
    """Per-turn loop detector.

    Construct one instance per agent turn (the executors do this implicitly).
    Call :meth:`check` *before* dispatching each tool; if it returns a
    non-``None`` :class:`AbortReason`, do not execute the tool — break the
    loop and surface the reason to the user.
    """

    max_calls: int = 50
    repeat_threshold: int = 3
    window_size: int = 5
    window_threshold: int = 3
    no_progress_threshold: int = 6
    disabled: bool = False
    session_id: Optional[str] = None
    history: list = field(default_factory=list)
    _recent: Deque[_CallRecord] = field(default_factory=deque)
    _consecutive_repeat: int = 0
    _last_hash: Optional[str] = None
    _consecutive_empty: int = 0
    aborted_reason: Optional[AbortReason] = None

    def __post_init__(self) -> None:
        from deile.config.settings import get_settings

        s = get_settings()
        if s.loop_guard_disabled:
            self.disabled = True
        if s.loop_guard_max_calls != 50:
            self.max_calls = max(1, s.loop_guard_max_calls)
        if s.loop_guard_repeat_threshold != 3:
            self.repeat_threshold = max(2, s.loop_guard_repeat_threshold)
        if s.loop_guard_window_size != 5:
            self.window_size = max(2, s.loop_guard_window_size)
        if s.loop_guard_window_threshold != 3:
            self.window_threshold = max(2, s.loop_guard_window_threshold)
        if s.loop_guard_no_progress != 6:
            self.no_progress_threshold = max(2, s.loop_guard_no_progress)
        # window_threshold cannot exceed window_size — clamp so users can't
        # make the rule unreachable by setting threshold > size.
        if self.window_threshold > self.window_size:
            self.window_threshold = self.window_size
        # _recent is built with the configured maxlen now that we have it.
        self._recent = deque(maxlen=self.window_size)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def check(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]],
    ) -> Optional[AbortReason]:
        """Validate that calling ``tool_name(args)`` is safe.

        Returns ``None`` if the call is fine. Returns an :class:`AbortReason`
        if the loop must break — the caller MUST honor the abort and not
        execute the tool.
        """
        if self.disabled:
            return None

        args_hash = self._hash_args(tool_name, args)
        args_preview = self._preview_args(args)
        total_after = len(self.history) + 1

        # Rule 1 — hard ceiling. We compare against the count *after* this
        # call would be added so the cap is "do not exceed", not "must
        # already have exceeded".
        if total_after > self.max_calls:
            reason = AbortReason(
                kind=AbortKind.MAX_CALLS,
                tool_name=tool_name,
                args_hash=args_hash,
                args_preview=args_preview,
                repeat_count=total_after,
                total_calls=total_after,
                detail=(
                    f"hard call ceiling exceeded: {total_after} > "
                    f"{self.max_calls}"
                ),
            )
            self._record_abort(reason)
            return reason

        # Rule 2 — identical consecutive repetition. Count how many times
        # *this exact hash* has been issued in a row, including the new
        # call. If we've already broken once on this hash, keep returning
        # the same reason for any further calls.
        consecutive = self._consecutive_repeat + 1 if args_hash == self._last_hash else 1
        if consecutive >= self.repeat_threshold:
            reason = AbortReason(
                kind=AbortKind.IDENTICAL_REPEAT,
                tool_name=tool_name,
                args_hash=args_hash,
                args_preview=args_preview,
                repeat_count=consecutive,
                total_calls=total_after,
                detail=(
                    f"identical (tool, args) issued {consecutive} times in a row; "
                    f"threshold={self.repeat_threshold}"
                ),
            )
            self._record_abort(reason)
            return reason

        # Rule 3 — sliding window. We don't yet need to mutate `_recent`;
        # peek at it as if we'd added the new call, then count.
        peek_window = list(self._recent) + [
            _CallRecord(tool_name=tool_name, args_hash=args_hash)
        ]
        # The deque is bounded; trim manually so the lookahead respects it.
        if len(peek_window) > self.window_size:
            peek_window = peek_window[-self.window_size :]
        same_in_window = sum(1 for r in peek_window if r.args_hash == args_hash)
        if same_in_window >= self.window_threshold:
            reason = AbortReason(
                kind=AbortKind.SLIDING_WINDOW,
                tool_name=tool_name,
                args_hash=args_hash,
                args_preview=args_preview,
                repeat_count=same_in_window,
                total_calls=total_after,
                detail=(
                    f"{same_in_window} identical calls inside a window of "
                    f"{len(peek_window)}; threshold={self.window_threshold}"
                ),
            )
            self._record_abort(reason)
            return reason

        # Rule 4 — no-progress streak. If the previous N tool calls all
        # returned empty/error results AND the model is still calling
        # tools, the conversation is going nowhere. We honor this even
        # before the call runs: the streak ended with the last result, so
        # if it's already past threshold, abort *now* before issuing call
        # N+1.
        if self._consecutive_empty >= self.no_progress_threshold:
            reason = AbortReason(
                kind=AbortKind.NO_PROGRESS,
                tool_name=tool_name,
                args_hash=args_hash,
                args_preview=args_preview,
                repeat_count=self._consecutive_empty,
                total_calls=total_after,
                detail=(
                    f"{self._consecutive_empty} consecutive empty/error results "
                    f"with the model still calling tools; threshold="
                    f"{self.no_progress_threshold}"
                ),
            )
            self._record_abort(reason)
            return reason

        # Safe to proceed — commit the call to history.
        record = _CallRecord(tool_name=tool_name, args_hash=args_hash)
        self.history.append(record)
        self._recent.append(record)
        if args_hash == self._last_hash:
            self._consecutive_repeat += 1
        else:
            self._consecutive_repeat = 1
        self._last_hash = args_hash
        return None

    def record_result(self, made_progress: bool) -> None:
        """Update the no-progress counter with the latest tool result.

        Pass ``made_progress=True`` if the tool returned non-empty,
        non-error data; pass ``False`` for empty success or for any
        error. The guard does not introspect the result itself — the
        caller decides what counts as progress, since "empty" depends
        on the tool (e.g., ``list_files`` returning ``[]`` is empty,
        but ``http_get`` returning ``""`` may also be empty).
        """
        if self.disabled:
            return
        if made_progress:
            self._consecutive_empty = 0
        else:
            self._consecutive_empty += 1

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_args(tool_name: str, args: Optional[Dict[str, Any]]) -> str:
        """Stable, order-insensitive hash of ``(tool_name, args)``.

        We use ``json.dumps(sort_keys=True, default=str)`` so unhashable
        values (paths, datetimes, custom objects) degrade to ``str(...)``
        rather than raising. SHA-256 truncated to 16 hex chars is a
        plenty-collision-resistant identifier for the few dozen calls
        per turn.
        """
        try:
            payload = json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            payload = repr(args)
        digest = hashlib.sha256(f"{tool_name}|{payload}".encode("utf-8")).hexdigest()
        return digest[:16]

    @staticmethod
    def _preview_args(args: Optional[Dict[str, Any]], max_chars: int = 80) -> str:
        """One-line preview suitable for UI / audit messages."""
        if not args:
            return "(no args)"
        try:
            preview = json.dumps(args, default=str, ensure_ascii=False, sort_keys=True)
        except Exception:
            preview = str(args)
        preview = preview.replace("\n", " ").replace("\r", " ")
        if len(preview) > max_chars:
            preview = preview[: max_chars - 1] + "…"
        return preview

    def _record_abort(self, reason: AbortReason) -> None:
        """Log + audit + cache the abort. Idempotent for the same hash."""
        # If we've already aborted on this hash, don't re-fire audit events.
        if (
            self.aborted_reason is not None
            and self.aborted_reason.args_hash == reason.args_hash
            and self.aborted_reason.kind is reason.kind
        ):
            return
        self.aborted_reason = reason
        logger.warning(
            "ToolLoopGuard: aborted turn — kind=%s tool=%s args_hash=%s "
            "repeat=%d total=%d detail=%s",
            reason.kind.value,
            reason.tool_name,
            reason.args_hash,
            reason.repeat_count,
            reason.total_calls,
            reason.detail,
        )
        # AuditEvent — best-effort, never raise from the guard. Keeping the
        # import inline avoids tying the guard to the security package at
        # module-import time (helpful when the guard is used in lightweight
        # tests that don't initialize audit logging).
        try:
            from deile.security.audit_logger import (AuditEventType,
                                                     SeverityLevel,
                                                     get_audit_logger)

            get_audit_logger().log_event(
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                severity=SeverityLevel.WARNING,
                actor="tool_loop_guard",
                resource=f"tool:{reason.tool_name}",
                action="abort_loop",
                result="aborted",
                details={
                    "kind": reason.kind.value,
                    "args_hash": reason.args_hash,
                    "args_preview": reason.args_preview,
                    "repeat_count": reason.repeat_count,
                    "total_calls": reason.total_calls,
                    "detail": reason.detail,
                    "session_id": self.session_id,
                },
                tool_name=reason.tool_name,
            )
        except Exception as exc:  # pragma: no cover — audit must never crash the loop
            logger.debug("ToolLoopGuard: audit logging failed: %s", exc)


def make_guard(session_id: Optional[str] = None) -> ToolLoopGuard:
    """Convenience factory used by the executors so each turn gets a fresh
    detector. Every loop site MUST construct a new guard per turn — sharing
    one across turns would leak state and falsely trigger aborts.
    """
    return ToolLoopGuard(session_id=session_id)


# ----------------------------------------------------------------------
# Helpers shared across the streaming / non-streaming integrations.
# ----------------------------------------------------------------------


def tool_result_made_progress(result: Any) -> bool:
    """Heuristic: did the given ToolResult-like object carry useful payload?

    A "no progress" result is one of:
    * status == ERROR (any error result),
    * status == SUCCESS but ``data`` is None / empty list / empty dict /
      empty string AND ``message`` is empty.

    Anything else counts as progress — the model has new information to
    consume on the next iteration.

    The function tolerates any shape (dataclass, dict, plain object) so it
    can be reused across providers without coupling to ``ToolResult``.
    """
    if result is None:
        return False
    # Prefer the canonical attribute-based shape (deile.tools.base.ToolResult).
    status = getattr(result, "status", None)
    if status is not None:
        try:
            from deile.tools.base import \
                ToolStatus  # local import to avoid cycle

            if status == ToolStatus.ERROR:
                return False
        except Exception:
            pass
    data = getattr(result, "data", None)
    message = getattr(result, "message", None)
    if data is None and not message:
        return False
    if isinstance(data, (list, dict, str, bytes)) and not data and not message:
        return False
    return True


def format_loop_break_message(reason: AbortReason) -> str:
    """Render the abort as a single user-visible string. Used by callers
    that need to embed the abort into the agent's text response."""
    return (
        f"[loop-break: {reason.kind.value}] {reason.user_message()}"
    )


def args_hash_for(tool_name: str, args: Optional[Dict[str, Any]]) -> str:
    """Public alias for the same hashing scheme the guard uses internally —
    useful in tests and observability code that needs to compare hashes
    without instantiating a guard.
    """
    return ToolLoopGuard._hash_args(tool_name, args)
