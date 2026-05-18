"""Shared issue-unlock logic for the pipeline ``reset`` operation (gap #34).

Both the ``/pipeline reset`` slash command and the ``pipeline`` tool need to
strip a pipeline's lock labels (``~batch:*`` and ``~by:*``) off an issue so it
can be re-processed. This module owns that algorithm; the two call sites only
format the outcome into their own response envelope.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from deile.orchestration.pipeline.github_client import GhCommandError
from deile.orchestration.pipeline.labels import BATCH_LABEL_PREFIX

_LOCK_LABEL_PREFIXES = (BATCH_LABEL_PREFIX, "~by:")


class UnlockResult(NamedTuple):
    """Outcome of :func:`unlock_issue`.

    ``ok`` is False only on a gh failure. When ``ok`` is True an empty
    ``removed`` means the issue carried no lock labels (a no-op success).
    """

    ok: bool
    removed: List[str]
    error: Optional[str]


async def unlock_issue(github, issue_number: int) -> UnlockResult:
    """Remove pipeline lock labels (``~batch:*``, ``~by:*``) from *issue_number*."""
    try:
        issue = await github.get_issue(issue_number)
    except GhCommandError as exc:
        return UnlockResult(False, [], f"gh error fetching issue #{issue_number}: {exc}")

    to_remove = [
        lb for lb in issue.labels
        if any(lb.startswith(prefix) for prefix in _LOCK_LABEL_PREFIXES)
    ]
    if not to_remove:
        return UnlockResult(True, [], None)

    try:
        await github.remove_labels("issue", issue_number, to_remove)
    except GhCommandError as exc:
        return UnlockResult(
            False, [], f"failed to remove labels from #{issue_number}: {exc}"
        )

    return UnlockResult(True, to_remove, None)
