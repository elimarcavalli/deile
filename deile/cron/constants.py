"""Central constants for the cron subsystem.

Deployment-tunable values are read from ``~/.deile/settings.json`` (or the
project-level ``.deile/settings.json``). DEILE_CRON_* env vars remain
supported as a deprecated fallback.
Truncation limits are pure Python constants.
"""
from __future__ import annotations

from deile.config.settings import get_settings


# ── CronRunner ────────────────────────────────────────────────────────────
#: Default polling cadence for :class:`CronRunner`.
def cron_poll_interval_seconds() -> int:
    """Live: re-reads settings at every call. Use this, NOT a frozen const.

    **NÃO ARMAZENE LOCALMENTE** — chamar esta função em loop infinito e guardar
    o resultado numa variável local reintroduziria o freeze que esta função
    existe para evitar. Chame-a a cada uso.
    """
    return get_settings().cron_poll_interval
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
