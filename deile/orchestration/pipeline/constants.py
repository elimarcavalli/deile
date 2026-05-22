"""Central constants for the autonomous pipeline.

Deployment-tunable values are read from ``~/.deile/settings.json`` (or the
project-level ``.deile/settings.json``). DEILE_PIPELINE_* env vars remain
supported as a deprecated fallback — set the JSON key instead.
Internal sizing limits are pure Python constants and are not intended to be
changed without a code review.
"""
from __future__ import annotations

from deile.config.settings import get_settings

# ── ClaudeDispatcher ──────────────────────────────────────────────────────
#: Maximum seconds a ``claude -p`` subprocess may run before it is killed.
CLAUDE_TIMEOUT_SECONDS: int = get_settings().pipeline_claude_timeout

# ── PipelineMonitor ───────────────────────────────────────────────────────
#: Default polling cadence for :class:`PipelineMonitor`.
PIPELINE_POLL_INTERVAL_SECONDS: int = get_settings().pipeline_poll_interval
#: Seconds ``stop()`` waits for the loop task before cancelling it.
PIPELINE_STOP_TIMEOUT_SECONDS: int = 5

# ── GitHub / pipeline repo ────────────────────────────────────────────────
#: Default ``owner/name`` when ``pipeline.repo`` is not set.
PIPELINE_DEFAULT_REPO: str = "elimarcavalli/deile"


def resolve_pipeline_repo() -> str:
    """Return the active ``owner/name`` for the pipeline repository.

    Reads ``pipeline_repo`` from `Settings` (which itself layers
    ``~/.deile/settings.json`` over project settings); falls back to
    `PIPELINE_DEFAULT_REPO`. Single source of truth for both the
    pipeline tool and the slash command — they used to inline the same
    expression independently.
    """
    return get_settings().pipeline_repo or PIPELINE_DEFAULT_REPO

# ── Prompt / message truncation ───────────────────────────────────────────
#: Max chars of issue body EMBEDDED in a worker brief. Kept well under the 8000
#: dispatch-payload cap so the brief (template + body) never overflows — the
#: worker reads the FULL live issue via ``gh issue view`` anyway, so the embedded
#: copy is just initial context. (A refined feature_request body can be large;
#: 6000 + the refine brief template overflowed 8000 — issue #257.)
ISSUE_BODY_MAX_CHARS: int = 5000
#: Max chars of stderr / error detail shown in Discord notifications.
PIPELINE_MSG_TRUNCATE_CHARS: int = 1500
