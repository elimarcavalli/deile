"""Central constants for the cron subsystem.

Deployment-tunable values are backed by env vars.  Truncation limits are
pure Python constants.
"""
from __future__ import annotations

import os

# ── CronRunner ────────────────────────────────────────────────────────────
#: Default polling cadence for :class:`CronRunner`.
CRON_POLL_INTERVAL_SECONDS: int = int(os.environ.get("DEILE_CRON_POLL_INTERVAL", "30"))
#: Seconds ``stop()`` waits for the loop task before cancelling it.
CRON_STOP_TIMEOUT_SECONDS: int = 5

# ── CronStore / result storage ────────────────────────────────────────────
#: Max chars persisted in ``last_result`` and error strings.
CRON_RESULT_MAX_CHARS: int = 500

# ── Discord DM notifications ──────────────────────────────────────────────
#: Max chars of ``entry.prompt`` shown in the notification DM.
CRON_DM_PROMPT_MAX_CHARS: int = 300
#: Max chars of the result summary shown in the notification DM.
CRON_DM_RESULT_MAX_CHARS: int = 1500
