"""Central constants for the autonomous pipeline.

Deployment-tunable values are backed by env vars so operators can override
them without code changes.  Internal sizing limits are pure Python constants
and are not intended to be changed without a code review.
"""
from __future__ import annotations

import os

# ── ClaudeDispatcher ──────────────────────────────────────────────────────
#: Maximum seconds a ``claude -p`` subprocess may run before it is killed.
CLAUDE_TIMEOUT_SECONDS: int = int(os.environ.get("DEILE_PIPELINE_CLAUDE_TIMEOUT", "1800"))

# ── PipelineMonitor ───────────────────────────────────────────────────────
#: Default polling cadence for :class:`PipelineMonitor`.
PIPELINE_POLL_INTERVAL_SECONDS: int = int(
    os.environ.get("DEILE_PIPELINE_POLL_INTERVAL", "60")
)
#: Seconds ``stop()`` waits for the loop task before cancelling it.
PIPELINE_STOP_TIMEOUT_SECONDS: int = 5

# ── GitHub / pipeline repo ────────────────────────────────────────────────
#: Default ``owner/name`` when ``DEILE_PIPELINE_REPO`` is not set.
PIPELINE_DEFAULT_REPO: str = "elimarcavalli/deile"

# ── Prompt / message truncation ───────────────────────────────────────────
#: Max chars of issue body sent to the implement prompt.
ISSUE_BODY_MAX_CHARS: int = 6000
#: Max chars of stderr / error detail shown in Discord notifications.
PIPELINE_MSG_TRUNCATE_CHARS: int = 1500
