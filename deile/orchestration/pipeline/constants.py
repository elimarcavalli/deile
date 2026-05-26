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

# ── Forge / pipeline repo ─────────────────────────────────────────────────
#: Default project path (``owner/repo`` on GH, ``group/.../project`` on GL)
#: when ``forge.repo`` / ``pipeline.repo`` is not set.
PIPELINE_DEFAULT_REPO: str = "elimarcavalli/deile"


def resolve_forge_repo() -> str:
    """Return the active project path for the pipeline repository.

    Accepts both shapes: GitHub ``owner/repo`` and GitLab
    ``group/(subgroup/)*project``. Reads ``forge.repo`` from
    :class:`Settings` first (new canonical), falling back to the legacy
    ``pipeline.repo`` for transitional compatibility, then to
    :data:`PIPELINE_DEFAULT_REPO`. Single source of truth for both the
    pipeline tool and the slash command — they used to inline the same
    expression independently.
    """
    settings = get_settings()
    return (
        getattr(settings, "forge_repo", "")
        or settings.pipeline_repo
        or PIPELINE_DEFAULT_REPO
    )


def resolve_pipeline_repo() -> str:
    """Deprecated alias for :func:`resolve_forge_repo`.

    Kept here so callers (especially tests) do not have to migrate in
    lock-step with the rename. New code should use
    :func:`resolve_forge_repo`.
    """
    return resolve_forge_repo()

# ── Prompt / message truncation ───────────────────────────────────────────
#: Max chars of issue body EMBEDDED in a worker brief. Kept well under the 8000
#: dispatch-payload cap so the brief (template + body) never overflows — the
#: worker reads the FULL live issue via ``gh issue view`` anyway, so the embedded
#: copy is just initial context. (A refined feature_request body can be large;
#: 6000 + the refine brief template overflowed 8000 — issue #257.)
ISSUE_BODY_MAX_CHARS: int = 5000
#: Max chars of stderr / error detail shown in Discord notifications.
PIPELINE_MSG_TRUNCATE_CHARS: int = 1500
