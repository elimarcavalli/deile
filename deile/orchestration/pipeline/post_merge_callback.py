"""Factory for the post-merge episodic memory callback."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def make_post_merge_callback(
    agent,
) -> Optional[Callable[[int, str, str], Awaitable[None]]]:
    """Return an async callback that stores a PR-merged episode in the agent's episodic memory.

    Returns None when agent is None (e.g. CLI one-shot mode) so callers can guard
    with ``if cb is not None``.
    """
    if agent is None:
        return None

    async def _cb(pr_number: int, pr_title: str, pr_url: str) -> None:
        mem = getattr(agent, "memory_manager", None)
        if mem is None:
            return
        episodic = getattr(mem, "episodic_memory", None)
        if episodic is None:
            return
        try:
            await episodic.store_episode(
                user_input=f"PR #{pr_number} merged: {pr_title}",
                agent_response="[pipeline:merge]",
                context={"type": "pr_merged", "pr_number": pr_number, "pr_url": pr_url},
                session_id=f"pipeline-merge-{pr_number}",
            )
            logger.debug("post-merge episodic memory stored for PR #%d", pr_number)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "post_merge_callback: episodic store failed for PR #%d: %s",
                pr_number,
                exc,
            )

    return _cb
