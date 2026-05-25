"""Backwards-compatibility shim — see :mod:`deile.orchestration.forge`.

Every symbol that used to live here has moved to the forge layer. This
module re-exports them so legacy imports keep working::

    # legacy
    from deile.orchestration.pipeline.github_client import GitHubClient, IssueRef
    # equivalent (preferred)
    from deile.orchestration.forge import GitHubForge as GitHubClient, IssueRef

The shim emits a :class:`DeprecationWarning` once per process at import
time. New code MUST import from ``deile.orchestration.forge`` directly.

Removal: one release after the forge layer ships (issue #297). The shim
exists so the migration is a refactor, not a coordinated breaking change
across the whole codebase.
"""

from __future__ import annotations

import warnings as _warnings

# The legacy public API mapped 1:1 onto the new forge layer.
from deile.orchestration.forge import (CommentRef, ForgeCommandError,
                                       GhCommandError, GitHubForge, IssueRef,
                                       MentionTrigger, PrRef,
                                       compute_batch_id_for_number)

# Historical class name — kept as a strict alias so ``isinstance`` checks
# in legacy callers still match.
GitHubClient = GitHubForge

_warnings.warn(
    "deile.orchestration.pipeline.github_client is deprecated; "
    "import from deile.orchestration.forge instead "
    "(GitHubForge replaces GitHubClient — same public API).",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "GitHubClient",
    "GitHubForge",
    "GhCommandError",
    "ForgeCommandError",
    "IssueRef",
    "PrRef",
    "CommentRef",
    "MentionTrigger",
    "compute_batch_id_for_number",
]
