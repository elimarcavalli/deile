"""Default review_callback factory for the autonomous pipeline (gap #2).

Stage 1 wires an LLM review of the issue body into the pipeline via a
callback.  When the caller does not supply a custom callback, this module
provides a sensible default: ask DEILE (the agent itself) to summarise the
issue and suggest an implementation approach, then return the text so the
monitor can post it as a comment.

The default callback is a *stub* if no agent is available — it returns an
empty string, which the monitor treats as "no comment to post".  Passing a
real :class:`~deile.core.agent.DeileAgent` (or any object with an
``async process(str) -> str`` method) activates the real review.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from deile.orchestration.pipeline.github_client import IssueRef

logger = logging.getLogger(__name__)


def make_review_callback(
    agent: Optional[Any] = None,
) -> Optional[Callable[[IssueRef], Awaitable[str]]]:
    """Return a review callback wired to *agent*, or None when no agent given.

    The returned coroutine receives an :class:`IssueRef` and returns a
    Markdown string to post as a comment on the issue (may be empty, which
    means "no comment").

    Parameters
    ----------
    agent:
        Any object with an ``async process(prompt: str) -> str`` method.
        Typically the live :class:`~deile.core.agent.DeileAgent` instance.
        If *None*, returns *None* so :class:`PipelineMonitor` skips the LLM
        step but still transitions the label.
    """
    if agent is None:
        return None

    process = getattr(agent, "process", None)
    if not callable(process):
        logger.warning(
            "make_review_callback: agent %r has no callable 'process' method; "
            "review_callback will be None",
            type(agent).__name__,
        )
        return None

    async def _callback(issue: IssueRef) -> str:
        prompt = (
            f"Você está revisando a issue #{issue.number}: **{issue.title}**.\n\n"
            f"Corpo da issue:\n{issue.body.strip()[:4000]}\n\n"
            f"Por favor:\n"
            f"1. Resuma o problema em 2–3 frases.\n"
            f"2. Sugira uma abordagem de implementação concisa (máx 5 pontos).\n"
            f"3. Identifique dependências ou riscos relevantes.\n\n"
            f"Responda em Markdown, sem cabeçalhos de nível 1. "
            f"Seja objetivo — este texto será postado como comentário na issue."
        )
        try:
            response = await process(prompt)
            return str(response).strip() if response else ""
        except Exception as exc:  # noqa: BLE001 — review is best-effort
            logger.warning(
                "review_callback for issue #%d failed: %s", issue.number, exc
            )
            return ""

    return _callback
