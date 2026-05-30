"""Backlog data collectors — bucketisation + forge fetch.

Extracted from ``backlog_command.py`` so the command file owns argument
parsing and Rich rendering, while these pure functions own the data
collection and bucket assignment. Mirrors the pattern set by
``_status_collectors.py`` and ``_standup_collectors.py``.

Pilar 03 §2 (Hexagonal): the forge transport lives in
:class:`ForgeClient` (resolved per-repo via :func:`get_forge_router`),
not here — this module never touches ``gh``/``glab`` directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from ...orchestration.forge import get_forge_router
from ...orchestration.forge.refs import IssueRef, PrRef
from ...orchestration.pipeline.labels import (REVIEW_LABELS, WORKFLOW_BLOCKED,
                                              WORKFLOW_LABELS,
                                              WORKFLOW_WAITING)

# Prefixes used to strip ``~workflow:`` / ``~review:`` off label constants
# when building bucket *display* names. The ``labels.py`` constants stay
# the single source of truth; we never re-declare the bucket strings.
_WORKFLOW_PREFIX = "~workflow:"
_REVIEW_PREFIX = "~review:"


def _strip_prefix(label: str, prefix: str) -> str:
    assert label.startswith(prefix), f"label {label!r} missing prefix {prefix!r}"
    return label[len(prefix):]


# Canonical ordered buckets derived from ``labels.py`` — any rename or
# addition in the pipeline-owned label tuple flows here automatically.
ISSUE_BUCKETS: Tuple[str, ...] = tuple(
    _strip_prefix(lb, _WORKFLOW_PREFIX) for lb in WORKFLOW_LABELS
)

# PRs: every ``~review:*`` bucket + the ``bloqueada`` overlay (which is a
# ``~workflow:`` label that may attach to a PR — see ``stages.py``).
PR_BUCKETS: Tuple[str, ...] = tuple(
    _strip_prefix(lb, _REVIEW_PREFIX) for lb in REVIEW_LABELS
) + (_strip_prefix(WORKFLOW_BLOCKED, _WORKFLOW_PREFIX),)

_SEM_WORKFLOW = "(sem ~workflow:*)"
_SEM_REVIEW = "(sem ~review:* e sem bloqueada)"

# Bucket display names for the two overlay/terminal labels — derived once so
# the bucket *name* keeps casing/spelling in lockstep with ``labels.py``.
_BLOCKED_BUCKET = _strip_prefix(WORKFLOW_BLOCKED, _WORKFLOW_PREFIX)
_WAITING_BUCKET = _strip_prefix(WORKFLOW_WAITING, _WORKFLOW_PREFIX)


@dataclass
class BacklogData:
    """Aggregated counts for the two backlog tables."""
    repo: str
    issue_counts: Dict[str, int] = field(default_factory=dict)
    pr_counts: Dict[str, int] = field(default_factory=dict)
    issue_total: int = 0
    pr_total: int = 0


def _bucket_issue(labels: Iterable[str]) -> str:
    """Assign an open issue to its backlog bucket.

    Precedence (mirrors ``_derive_workflow`` in ``infra/k8s/_panel_data.py``
    plus the ``aguardando_stakeholder`` overlay rule from issue #419):

    1. ``WORKFLOW_BLOCKED`` → **bloqueada** (terminal, overrides everything)
    2. ``WORKFLOW_WAITING`` → **aguardando_stakeholder** (overlay)
    3. First ``~workflow:*`` in canonical ``WORKFLOW_LABELS`` order → bucket
    4. No ``~workflow:*`` present → ``_SEM_WORKFLOW``
    """
    labels_set = set(labels)
    if not any(lb.startswith(_WORKFLOW_PREFIX) for lb in labels_set):
        return _SEM_WORKFLOW
    if WORKFLOW_BLOCKED in labels_set:
        return _BLOCKED_BUCKET
    if WORKFLOW_WAITING in labels_set:
        return _WAITING_BUCKET
    for canonical in WORKFLOW_LABELS:
        if canonical in labels_set:
            return _strip_prefix(canonical, _WORKFLOW_PREFIX)
    # Defensive fallback: a ``~workflow:*`` label not in WORKFLOW_LABELS.
    for lb in labels_set:
        if lb.startswith(_WORKFLOW_PREFIX):
            return _strip_prefix(lb, _WORKFLOW_PREFIX)
    return _SEM_WORKFLOW  # unreachable


def _bucket_pr(labels: Iterable[str]) -> str:
    """Assign an open PR to its backlog bucket.

    Precedence:
    1. ``WORKFLOW_BLOCKED`` → **bloqueada** (overrides any ``~review:*``)
    2. First ``~review:*`` in canonical ``REVIEW_LABELS`` order → bucket
    3. Neither → ``_SEM_REVIEW``
    """
    labels_set = set(labels)
    if WORKFLOW_BLOCKED in labels_set:
        return _BLOCKED_BUCKET
    for canonical in REVIEW_LABELS:
        if canonical in labels_set:
            return _strip_prefix(canonical, _REVIEW_PREFIX)
    return _SEM_REVIEW


def bucketize_issues(issues: List[IssueRef]) -> Dict[str, int]:
    """Aggregate *issues* into a count-by-bucket dict.

    All canonical buckets are pre-populated with ``0`` so empty states are
    still visible in the rendered table. ``_SEM_WORKFLOW`` is added only
    when at least one issue lacks a ``~workflow:*`` label.
    """
    counts: Dict[str, int] = {b: 0 for b in ISSUE_BUCKETS}
    for issue in issues:
        bucket = _bucket_issue(issue.labels)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def bucketize_prs(prs: List[PrRef]) -> Dict[str, int]:
    """Aggregate *prs* into a count-by-bucket dict (mirror of bucketize_issues)."""
    counts: Dict[str, int] = {b: 0 for b in PR_BUCKETS}
    for pr in prs:
        bucket = _bucket_pr(pr.labels)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


async def collect_backlog_data(repo: str) -> BacklogData:
    """Fetch all open issues/PRs for *repo* and aggregate by workflow bucket.

    Forge-agnostic: the :class:`ForgeRouter` picks GitHub or GitLab based
    on the configured environment (Decisão #42). Issues and PRs are
    fetched in parallel via ``asyncio.gather`` — both calls are paginated
    by the adapter with a 1000-item ceiling (sufficient for any active
    project; the rare project that exceeds this cap is documented here
    rather than silently truncated).
    """
    forge = get_forge_router().route(project_path=repo)
    issues, prs = await asyncio.gather(
        forge.list_open_issues(limit=1000),
        forge.list_open_prs(limit=1000),
    )
    return BacklogData(
        repo=repo,
        issue_counts=bucketize_issues(issues),
        pr_counts=bucketize_prs(prs),
        issue_total=len(issues),
        pr_total=len(prs),
    )
