"""Autonomous DEILE-bot → DEILE → Claude Code pipeline.

This package implements intent #87 (elimarcavalli/deile#87): a continuous
3-agent loop that turns Discord-submitted ideas into merged code changes.

Components
----------
labels            Constants for ~workflow:*, ~review:*, ~batch:* labels.
github_client     `gh` CLI wrapper for issue/PR/label operations.
worktree_manager  `.worktrees/<branch>` setup for isolated implementation.
claude_dispatcher Subprocess invocation of `claude -p "<prompt>"`.
notifier          Discord-DM notifier that ties to elimar.ciss.
monitor           1-minute polling loop that drives the whole pipeline.
"""

from deile.orchestration.pipeline.labels import (BATCH_LABEL_PREFIX,
                                                 REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING, WORKFLOW_NEW,
                                                 WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)

__all__ = [
    "BATCH_LABEL_PREFIX",
    "WORKFLOW_NEW",
    "WORKFLOW_REVIEWING",
    "WORKFLOW_REVIEWED",
    "WORKFLOW_PR",
    "REVIEW_PENDING",
    "REVIEW_IN_PROGRESS",
    "REVIEW_CONCLUDED",
]
