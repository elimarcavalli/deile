"""Cron → Agent bridge for intent #86 (autonomous pipeline).

This module closes the loop between the CronRunner and the DEILE agent:
the CronRunner fires a scheduled entry by calling a ``FireCallback``; this
module provides ``make_fire_callback``, which manufactures that callback
from a lazy ``agent_provider`` factory.

Integration point
-----------------
CronRunner receives a ``fire_callback: FireCallback`` at construction time.
Callers that bootstrap the full agent stack should wire it like this::

    from deile.cron.agent_bridge import make_fire_callback

    async def agent_provider():
        return my_deile_agent  # DeileAgent instance

    runner = CronRunner(store, fire_callback=make_fire_callback(agent_provider))

Design notes
------------
- ``agent_provider`` is a **lazy async factory** so the bridge does not
  require the agent to be initialised at module import time.  The provider
  is called once per ``tick``; callers may cache the agent themselves or
  return a freshly-built one — both are valid patterns.
- All exceptions are caught and converted to a human-readable error string
  so the runner can persist a ``last_result`` without raising.  The runner
  itself already guards its outer loop, but defence-in-depth here prevents
  a single bad entry from surfacing an unhandled exception.
- The ``session_id`` format ``"cron-{entry.id}"`` deliberately encodes the
  entry id so agent memory layers can correlate turns across recurring fires.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from deile.cron.runner import FireCallback
from deile.cron.store import CronEntry

logger = logging.getLogger(__name__)

AgentProvider = Callable[[], Awaitable[Any]]


def make_fire_callback(
    agent_provider: AgentProvider,
    *,
    max_summary_chars: int = 500,
) -> FireCallback:
    """Return a :data:`FireCallback` that routes a :class:`CronEntry` through DEILE.

    Parameters
    ----------
    agent_provider:
        Async factory that returns a :class:`~deile.core.agent.DeileAgent`
        (or any object with ``async process_input(prompt, session_id=...)``).
        Called on every fire so the caller controls caching / lifecycle.
    max_summary_chars:
        Maximum length of the returned summary string.  Longer responses
        are truncated with an ellipsis.  Defaults to 500.

    Returns
    -------
    FireCallback
        An ``async (entry: CronEntry) -> str`` callable ready to pass to
        :class:`~deile.cron.runner.CronRunner`.
    """

    async def _fire(entry: CronEntry) -> str:
        try:
            agent = await agent_provider()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cron agent_provider failed for entry %s: %s",
                entry.id,
                exc,
                exc_info=True,
            )
            return f"error: {type(exc).__name__}: {exc}"

        try:
            response = await agent.process_input(
                entry.prompt,
                session_id=f"cron-{entry.id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cron agent.process_input failed for entry %s: %s",
                entry.id,
                exc,
                exc_info=True,
            )
            return f"error: {type(exc).__name__}: {exc}"

        # Extract text: prefer .content attribute (AgentResponse), fall back
        # to str() for duck-typed responses.
        try:
            text = response.content if hasattr(response, "content") else str(response)
        except Exception:  # noqa: BLE001
            text = ""

        if not text:
            text = str(response) if response is not None else ""

        summary = text[:max_summary_chars]
        if len(text) > max_summary_chars:
            summary = summary[: max_summary_chars - 1] + "…"

        logger.debug(
            "cron entry %s fired; summary length=%d", entry.id, len(summary)
        )
        return summary

    return _fire
