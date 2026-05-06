"""Generic task scheduler for DEILE — implements intent #86.

Lets a user ask "lembra de me mandar o relatório toda segunda às 9h" and
have DEILE persist + execute that prompt at the right time. Distinct from
``deile/orchestration/pipeline/scheduler.py``, which schedules pipeline
*stages* (review/implement/pr_review). This package schedules **arbitrary
natural-language prompts** that get fed back into the agent on fire.

Components:
    store    — CronEntry + CronStore (SQLite-backed persistence)
    runner   — CronRunner async loop that polls, fires entries, captures result

The three LLM-callable tools (CronCreate / CronList / CronDelete) live
under ``deile/tools/`` for registry auto-discovery.
"""

from deile.cron.store import CronEntry, CronStore

__all__ = ["CronEntry", "CronStore"]
