"""Terminal GC — removes transient pipeline labels from closed/merged items.

Called when an issue transitions to 'closed' or a PR transitions to
'closed'/'merged'. Eliminates label accumulation on terminal work items
without touching priority or project-classification labels.

Standalone module: importable without loading stages.py or the full
pipeline graph.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List, Literal, Tuple

from deile.orchestration.pipeline.labels import (
    BY_LABEL_PREFIX,
    FOLLOW_UPS_PROCESSED,
    MENTION_DONE,
    REFINAR,
    WORKFLOW_CONCLUDED,
    WORKFLOW_DECOMPOSED,
    is_attempt_label,
    is_batch_label,
    is_refine_attempt_label,
)

if TYPE_CHECKING:
    from deile.orchestration.forge.base import ForgeClient

logger = logging.getLogger(__name__)

_WORKFLOW_PREFIX = "~workflow:"
_REVIEW_PREFIX = "~review:"


class GCOnOpenItemError(Exception):
    """Raised when run_terminal_gc is called on an open item.

    Zero API calls are made before this exception is raised.
    """


def _to_strip_issue(current_labels: Tuple[str, ...]) -> List[str]:
    """Labels to remove from a closed issue."""
    result = []
    for lb in current_labels:
        if lb.startswith(_WORKFLOW_PREFIX):
            if lb not in (WORKFLOW_DECOMPOSED, WORKFLOW_CONCLUDED):
                result.append(lb)
        elif lb.startswith(BY_LABEL_PREFIX):
            result.append(lb)
        elif is_batch_label(lb):
            result.append(lb)
        elif is_attempt_label(lb):
            result.append(lb)
        elif is_refine_attempt_label(lb):
            result.append(lb)
        elif lb == MENTION_DONE:
            result.append(lb)
        elif lb == REFINAR:
            result.append(lb)
    return result


def _to_strip_pr(current_labels: Tuple[str, ...]) -> List[str]:
    """Labels to remove from a closed or merged PR."""
    result = []
    for lb in current_labels:
        if lb.startswith(_REVIEW_PREFIX):
            result.append(lb)
        elif lb.startswith(BY_LABEL_PREFIX):
            result.append(lb)
        elif is_batch_label(lb):
            result.append(lb)
        elif is_attempt_label(lb):
            result.append(lb)
        elif lb.startswith(_WORKFLOW_PREFIX):
            result.append(lb)
        elif lb == FOLLOW_UPS_PROCESSED:
            result.append(lb)
    return result


async def run_terminal_gc(
    forge: "ForgeClient",
    item_type: Literal["issue", "pr"],
    item_number: int,
    item_state: Literal["open", "closed", "merged"],
    *,
    api_timeout_s: float = 10.0,
) -> Literal["success", "noop", "partial"]:
    """Remove transient pipeline labels from a terminal item.

    Returns:
        'success'  — all planned mutations executed successfully
        'noop'     — no mutations needed (item already GC'd or non-terminal)
        'partial'  — at least one mutation failed; failures logged individually

    Raises:
        GCOnOpenItemError — if item_state == 'open' (zero API calls made)

    Concurrency note: sequential invocations guarantee idempotency ('noop'
    on second call). Concurrent invocations are safe — both complete without
    unhandled exceptions — but the second returns 'success' rather than 'noop'
    because its API calls receive silent 404s (github_forge.py:977-978).

    ~prioridade:* labels are never removed regardless of item type or state.
    """
    if item_state == "open":
        raise GCOnOpenItemError(
            f"run_terminal_gc called on open {item_type} #{item_number}; "
            "GC only applies to terminal items (closed/merged)"
        )

    if item_type == "issue":
        ref = await asyncio.wait_for(
            forge.get_issue(item_number), timeout=api_timeout_s
        )
        current: Tuple[str, ...] = ref.labels if ref is not None else ()
    else:
        ref = await asyncio.wait_for(forge.get_pr(item_number), timeout=api_timeout_s)
        current = ref.labels if ref is not None else ()

    if item_type == "issue":
        to_remove = _to_strip_issue(current)
        to_add = [] if WORKFLOW_CONCLUDED in current else [WORKFLOW_CONCLUDED]
    else:
        to_remove = _to_strip_pr(current)
        to_add = []

    if not to_remove and not to_add:
        logger.debug(
            "run_terminal_gc: %s #%d already clean — noop",
            item_type,
            item_number,
        )
        return "noop"

    failures = 0

    if to_remove:
        try:
            await asyncio.wait_for(
                forge.remove_labels(item_type, item_number, to_remove),
                timeout=api_timeout_s,
            )
            logger.debug(
                "run_terminal_gc: removed %s from %s #%d",
                to_remove,
                item_type,
                item_number,
            )
        except Exception as exc:
            logger.warning(
                "run_terminal_gc: remove_labels failed for %s #%d: %s",
                item_type,
                item_number,
                exc,
            )
            failures += 1

    if to_add:
        try:
            await asyncio.wait_for(
                forge.add_labels(item_type, item_number, to_add),
                timeout=api_timeout_s,
            )
            logger.debug(
                "run_terminal_gc: added %s to %s #%d",
                to_add,
                item_type,
                item_number,
            )
        except Exception as exc:
            logger.warning(
                "run_terminal_gc: add_labels failed for %s #%d: %s",
                item_type,
                item_number,
                exc,
            )
            failures += 1

    return "partial" if failures > 0 else "success"


__all__ = ["GCOnOpenItemError", "run_terminal_gc"]
