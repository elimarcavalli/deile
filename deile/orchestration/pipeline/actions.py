"""Canonical registry of pipeline actions.

Three sites used to maintain parallel tables of the action names the
pipeline understands:

- ``scheduler.VALID_ACTIONS`` (used by ``RecurringEntry``/``OneshotEntry``
  validation),
- ``monitor._run_scheduled()`` (an if/elif chain + ``_ENABLE_FLAGS`` dict),
- ``PipelineScheduleTool.parameters['trigger_action'].enum`` (exposed to
  the LLM).

They drifted: ``VALID_ACTIONS`` and the tool enum each missed a couple of
the seven actions the monitor actually dispatches. A scheduled
``pr_triage`` / ``mention_handling`` entry was rejected by
``RecurringEntry.__post_init__`` even though the monitor knows how to
run it. Centralising the table here eliminates that class of drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class ActionDef:
    """Static metadata for one pipeline action.

    Attributes
    ----------
    name:
        Public action name (used in YAML schedules and in the LLM tool
        ``trigger_action`` enum).
    method:
        Name of the ``PipelineMonitor`` coroutine method that implements
        the action — looked up via ``getattr`` so this module stays free
        of an import cycle on ``PipelineMonitor``.
    enable_attr:
        Name of the ``PipelineConfig`` boolean attribute that gates the
        action. ``getattr(config, enable_attr)`` must return ``True`` for
        the monitor to actually invoke the method.
    """

    name: str
    method: str
    enable_attr: str


# Note: ``mention_handling`` keeps its own ``_mention_cursor_path`` and is
# designed to run on every poll tick. Scheduling it via cron works but
# means two cursors compete (the schedule's ``last_run_at`` and the
# on-disk cursor) — prefer leaving it to the polling loop.
ACTIONS: Tuple[ActionDef, ...] = (
    ActionDef("classify", "_classify_new_issues", "enable_classify"),
    ActionDef("review", "_review_one_new_issue", "enable_review"),
    ActionDef("implement", "_implement_one_reviewed_issue", "enable_implement"),
    ActionDef("pr_review", "_review_one_open_pr", "enable_pr_review"),
    ActionDef("pr_triage", "_classify_new_prs", "enable_pr_triage"),
    # ``mention_handling`` also has its own polling cursor
    # (``_mention_cursor_path``) driven by the tick loop.  Scheduling it via
    # a recurring/oneshot entry runs the same handler from the scheduler
    # path; both paths share the cursor so duplicate processing is
    # prevented, but the action will fire from BOTH places when scheduled.
    ActionDef("mention_handling", "_process_mentions", "enable_mention_handling"),
    ActionDef("follow_ups", "_standalone_follow_ups", "enable_follow_ups"),
)

ACTIONS_BY_NAME: Dict[str, ActionDef] = {a.name: a for a in ACTIONS}

# Tuple (not frozenset) so the order is deterministic — useful for
# LLM-facing enum listings.
ACTION_NAMES: Tuple[str, ...] = tuple(a.name for a in ACTIONS)
