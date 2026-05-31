"""dispatch_logger — structured dispatch event helper for deile-worker / claude-worker.

Emits one log line per event to ``logging.getLogger("deile.dispatch")``.
Format: ``<event_name> key=value key=value …``
- Bold/required keys must always be present.
- Optional keys are omitted entirely when absent (never emitted as ``key=None``).

Health-probe throttle: :func:`log_health_probe` suppresses duplicate lines for
the same path within a 30-second window so probe noise doesn't drown the log.

Wire contract: key names here are the canonical names consumed by
``_panel_data.WorkerProvider._parse`` and the panel TUI.  Changes must be
mirrored in ``_DISPATCH_STARTED_RE`` / ``_DISPATCH_COMPLETED_RE`` there.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

_logger = logging.getLogger("deile.dispatch")

# Health-probe throttle: path -> last-logged epoch (float seconds).
_probe_last: dict[str, float] = {}
_PROBE_THROTTLE_S: float = 30.0


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _emit(event: str, **kv) -> None:
    """Emit one structured log line: ``event key=v key=v …``

    None values are silently dropped so callers can always pass optional
    kwargs and the wire stays clean.
    """
    parts = [event]
    for k, v in kv.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    _logger.info(" ".join(parts))


# ---------------------------------------------------------------------------
# Health-probe throttle
# ---------------------------------------------------------------------------

def log_health_probe(path: str, status: int) -> None:
    """Log an HTTP health probe at most once per :data:`_PROBE_THROTTLE_S` per path."""
    now = time.monotonic()
    last = _probe_last.get(path, 0.0)
    if now - last < _PROBE_THROTTLE_S:
        return
    _probe_last[path] = now
    _emit("health.probe", path=path, status=status)


# ---------------------------------------------------------------------------
# Dispatch lifecycle events
# ---------------------------------------------------------------------------

def dispatch_received(
    *,
    task: str,
    channel: str,
    stage: Optional[str] = None,
    issue: Optional[int] = None,
    pr: Optional[int] = None,
    kind: Optional[str] = None,
    branch: Optional[str] = None,
    persona: Optional[str] = None,
    model_requested: Optional[str] = None,
    effort: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Emit ``dispatch.received`` — once per dispatch after payload validation."""
    _emit(
        "dispatch.received",
        task=task,
        channel=channel,
        stage=stage,
        issue=issue,
        pr=pr,
        kind=kind,
        branch=branch,
        persona=persona,
        model_requested=model_requested,
        effort=effort,
        source=source,
    )


def dispatch_model_resolved(
    *,
    task: str,
    model: str,
    source: str,
    reasoning: Optional[str] = None,
) -> None:
    """Emit ``dispatch.model_resolved`` after the model tier is selected."""
    _emit(
        "dispatch.model_resolved",
        task=task,
        model=model,
        source=source,
        reasoning=reasoning,
    )


def dispatch_progress(
    *,
    task: str,
    elapsed_s: float,
    turn: Optional[int] = None,
    tool_last: Optional[str] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    pid: Optional[int] = None,
) -> None:
    """Emit ``dispatch.progress`` — periodic heartbeat during long-running tasks."""
    _emit(
        "dispatch.progress",
        task=task,
        elapsed_s=round(elapsed_s, 1),
        turn=turn,
        tool_last=tool_last,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        pid=pid,
    )


def dispatch_tool_burst(
    *,
    task: str,
    window_s: float,
    tools: str,
) -> None:
    """Emit ``dispatch.tool_burst`` — summary of tool calls in a window.

    *tools* format: ``Edit:5,Bash:3,Read:12`` (name:count CSV).
    """
    _emit(
        "dispatch.tool_burst",
        task=task,
        window_s=round(window_s, 1),
        tools=tools,
    )


def dispatch_completed(
    *,
    task: str,
    ok: bool,
    turns: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    duration_s: Optional[float] = None,
) -> None:
    """Emit ``dispatch.completed`` — terminal event on successful completion."""
    _emit(
        "dispatch.completed",
        task=task,
        ok=ok,
        turns=turns,
        cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_s=round(duration_s, 1) if duration_s is not None else None,
    )


def dispatch_failed(
    *,
    task: str,
    reason: str,
    turns: Optional[int] = None,
    duration_s: Optional[float] = None,
    error_code: Optional[str] = None,
) -> None:
    """Emit ``dispatch.failed`` — terminal event on failure / timeout / cancellation."""
    _emit(
        "dispatch.failed",
        task=task,
        reason=reason,
        turns=turns,
        duration_s=round(duration_s, 1) if duration_s is not None else None,
        error_code=error_code,
    )


# ---------------------------------------------------------------------------
# Git events
# ---------------------------------------------------------------------------

def git_commit(
    *,
    task: str,
    sha: str,
    branch: str,
    files: Optional[int] = None,
    plus: Optional[int] = None,
    minus: Optional[int] = None,
) -> None:
    """Emit ``git.commit`` after a successful git commit."""
    _emit(
        "git.commit",
        task=task,
        sha=sha,
        branch=branch,
        files=files,
        plus=plus,
        minus=minus,
    )


def git_push(
    *,
    task: str,
    branch: str,
    sha: str,
) -> None:
    """Emit ``git.push`` after a successful git push."""
    _emit(
        "git.push",
        task=task,
        branch=branch,
        sha=sha,
    )


# ---------------------------------------------------------------------------
# Forge events
# ---------------------------------------------------------------------------

def forge_pr_open(
    *,
    task: str,
    pr: int,
    url: str,
) -> None:
    """Emit ``forge.pr_open`` after a PR/MR is created."""
    _emit(
        "forge.pr_open",
        task=task,
        pr=pr,
        url=url,
    )


def forge_pr_review(
    *,
    task: str,
    pr: int,
    decision: str,
) -> None:
    """Emit ``forge.pr_review`` after a PR review is submitted.

    *decision* ∈ ``APPROVED`` | ``CHANGES_REQUESTED`` | ``COMMENTED``.
    """
    _emit(
        "forge.pr_review",
        task=task,
        pr=pr,
        decision=decision,
    )


def forge_pr_merge(
    *,
    task: str,
    pr: int,
    sha: str,
) -> None:
    """Emit ``forge.pr_merge`` after a PR/MR is merged."""
    _emit(
        "forge.pr_merge",
        task=task,
        pr=pr,
        sha=sha,
    )
