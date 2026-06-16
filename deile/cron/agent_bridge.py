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
- The prompt is wrapped in a ``[CRON FIRE]`` envelope so the agent knows
  the turn is a scheduled fire — without it the LLM tends to ask for
  context ("which channel?", "which user?") because a bare prompt like
  "me lembre de tomar café" reads as an unfinished request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from deile.cron.runner import FireCallback
from deile.cron.store import CronEntry

logger = logging.getLogger(__name__)

AgentProvider = Callable[[], Awaitable[Any]]


def _wrap_prompt(entry: CronEntry) -> str:
    """Wrap the raw entry prompt with a self-contained cron envelope.

    The LLM sees a clear marker ([CRON FIRE]) plus everything it needs to
    answer without asking back: who scheduled it, when it was scheduled
    for, and where the response will be delivered (DM by the runner). The
    envelope explicitly tells the model NOT to call tools and NOT to ask
    for clarification — the response itself IS the reminder text.
    """
    fired_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    scheduled_for = (
        entry.next_fire_at.strftime("%Y-%m-%d %H:%M UTC")
        if entry.next_fire_at
        else (entry.run_at.strftime("%Y-%m-%d %H:%M UTC") if entry.run_at else "?")
    )
    notify = entry.notify_user_id or "(none)"
    by = entry.created_by or "(unknown)"

    return (
        "[CRON FIRE] Esta é uma tarefa AGENDADA que disparou agora — não foi "
        "uma mensagem nova do usuário.\n"
        f"- entry_id: {entry.id}\n"
        f"- fired_at: {fired_at}\n"
        f"- scheduled_for: {scheduled_for}\n"
        f"- created_by: {by}\n"
        f"- notify_user_id: {notify}\n"
        "\n"
        "Sua resposta de TEXTO será entregue diretamente por DM ao "
        "notify_user_id pelo CronRunner. Comporte-se como o lembrete em si:\n"
        "- responda em PT-BR, curto e direto;\n"
        "- NÃO chame tools de mensageria (discord_send_dm, discord_react, etc) — "
        "o runner já cuida da entrega;\n"
        "- NÃO peça clarificação (não há ninguém pra responder agora);\n"
        "- se a tarefa pede ação no sistema (executar, instalar, ler arquivo), "
        "AÍ pode chamar `dispatch_deile_task` — caso contrário responda apenas "
        "com o texto do lembrete.\n"
        "\n"
        f"Conteúdo do agendamento (prompt original do usuário):\n{entry.prompt}"
    )


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

        wrapped_prompt = _wrap_prompt(entry)
        logger.info(
            "cron firing entry %s — notify_user_id=%s prompt_chars=%d",
            entry.id,
            entry.notify_user_id,
            len(entry.prompt),
        )

        try:
            response = await agent.process_input(
                wrapped_prompt,
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

        logger.debug("cron entry %s fired; summary length=%d", entry.id, len(summary))
        return summary

    return _fire
