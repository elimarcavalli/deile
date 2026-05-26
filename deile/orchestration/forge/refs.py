"""Forge-agnostic reference dataclasses.

These dataclasses describe issues, pull/merge requests, comments and the
mention triggers that the autonomous pipeline operates on. They are
intentionally **independent** of which forge (GitHub or GitLab) produced
the data — the same ``IssueRef`` shape carries an issue whether it came
from ``gh issue view`` or ``glab issue view``.

Migration note: these types used to live in
``deile.orchestration.pipeline.github_client``. They are re-exported from
there for backward compatibility (with a :class:`DeprecationWarning` at
the module level).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from deile.orchestration.pipeline.labels import (batch_id_from_label,
                                                 is_batch_label)


def _labels_from_payload(item: dict) -> Tuple[str, ...]:
    """Extract label names from a forge JSON payload.

    Tolerates both the object form (``[{"name": ...}]`` — typical of ``gh``
    ``--json labels`` and GitLab API responses with ``--json``) and the bare
    string form (``["bug", ...]`` — emitted by some ``gh api --jq`` shapes
    and by GitLab API in its raw labels field).
    """
    out: List[str] = []
    for lab in item.get("labels", []):
        if isinstance(lab, dict):
            name = lab.get("name")
            if name:
                out.append(str(name))
        elif isinstance(lab, str):
            out.append(lab)
    return tuple(out)


def _first_batch_id(labels: Tuple[str, ...]) -> Optional[str]:
    """Return the first ``~batch:<sha>`` id present in *labels*, else None."""
    return next(
        (batch_id_from_label(lb) for lb in labels if is_batch_label(lb)),
        None,
    )


@dataclass(frozen=True)
class IssueRef:
    """A forge-agnostic reference to an issue.

    ``number`` is the user-visible identifier (``#42`` on GitHub, ``#42`` on
    GitLab where it is the project-internal ``iid``). ``url`` is the public
    web URL — different shapes per forge, but always navigable in a browser.
    """

    number: int
    title: str
    url: str
    labels: Tuple[str, ...]
    body: str = ""
    state: str = "open"
    author: str = ""

    @property
    def batch_id(self) -> Optional[str]:
        return _first_batch_id(self.labels)

    @classmethod
    def from_gh_json(cls, item: dict) -> "IssueRef":
        author = item.get("author") or {}
        return cls(
            number=int(item["number"]),
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            labels=_labels_from_payload(item),
            body=str(item.get("body") or ""),
            state=str(item.get("state", "open")),
            author=str(author.get("login", "")) if isinstance(author, dict) else "",
        )

    @classmethod
    def from_gl_json(cls, item: dict) -> "IssueRef":
        """Build an :class:`IssueRef` from a GitLab REST API payload.

        Maps GitLab field names (``iid``, ``description``, ``web_url``,
        ``author.username``, ``labels`` as bare strings) onto the canonical
        names. GitLab uses ``iid`` for the project-visible number — that is
        what the operator sees in the UI and in URLs.
        """
        author = item.get("author") or {}
        # GitLab uses 'closed' for closed issues — normalise to the same
        # vocabulary the pipeline already uses ('open' / 'closed').
        state = str(item.get("state", "opened"))
        if state == "opened":
            state = "open"
        return cls(
            number=int(item.get("iid") or item.get("number") or 0),
            title=str(item.get("title", "")),
            url=str(item.get("web_url") or item.get("url") or ""),
            labels=_labels_from_payload(item),
            body=str(item.get("description") or item.get("body") or ""),
            state=state,
            author=str(author.get("username") or author.get("login") or ""),
        )


@dataclass(frozen=True)
class PrRef:
    """A forge-agnostic reference to a PR (GitHub) or MR (GitLab).

    The pipeline never branches on which one — the same handler labels,
    reviews, merges. ``head_ref``/``base_ref`` are the source/target branch
    names; ``is_draft`` reflects either GitHub's ``isDraft`` boolean OR
    GitLab's ``draft`` boolean (with ``Draft:`` / ``WIP:`` title prefix
    normalised away by the GitLab adapter).
    """

    number: int
    title: str
    url: str
    labels: Tuple[str, ...]
    head_ref: str = ""
    base_ref: str = "main"
    state: str = "open"
    is_draft: bool = False

    @property
    def batch_id(self) -> Optional[str]:
        return _first_batch_id(self.labels)

    @classmethod
    def from_gh_json(cls, item: dict, *, default_state: str = "open") -> "PrRef":
        return cls(
            number=int(item["number"]),
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            labels=_labels_from_payload(item),
            head_ref=str(item.get("headRefName") or ""),
            base_ref=str(item.get("baseRefName") or "main"),
            state=str(item.get("state", default_state)),
            is_draft=bool(item.get("isDraft", False)),
        )

    @classmethod
    def from_gl_json(cls, item: dict, *, default_state: str = "open") -> "PrRef":
        """Build a :class:`PrRef` from a GitLab MR REST API payload.

        Maps ``source_branch``→``head_ref``, ``target_branch``→``base_ref``,
        and converts GitLab's ``state`` vocabulary (``opened``/``closed``/
        ``merged``/``locked``) to the canonical pipeline vocabulary
        (``open``/``closed``/``merged``).
        """
        state = str(item.get("state", default_state))
        if state == "opened":
            state = "open"
        title = str(item.get("title", ""))
        # GitLab uses 'Draft:' / 'WIP:' title prefix in addition to the
        # ``draft`` boolean. Honour either signal so the pipeline triage stays
        # consistent with what a human operator sees in the UI.
        is_draft = bool(item.get("draft") or item.get("work_in_progress"))
        if not is_draft and (title.lower().startswith("draft:") or title.lower().startswith("wip:")):
            is_draft = True
        return cls(
            number=int(item.get("iid") or item.get("number") or 0),
            title=title,
            url=str(item.get("web_url") or item.get("url") or ""),
            labels=_labels_from_payload(item),
            head_ref=str(item.get("source_branch") or ""),
            base_ref=str(item.get("target_branch") or "main"),
            state=state,
            is_draft=is_draft,
        )


# Alias for callers who prefer the GitLab vocabulary ("MR" instead of "PR").
# The same dataclass — only the name differs. Lets ``from forge import MrRef``
# read naturally in GitLab-specific code paths.
MrRef = PrRef


@dataclass(frozen=True)
class CommentRef:
    """A comment on an issue or PR/MR, returned by ``list_*_comments_since``.

    The shape is identical across forges; the only distinguishing field is
    ``kind`` (``"issue"`` vs ``"pr_review"``) which the mention router uses
    to decide where the comment lives.
    """

    comment_id: int
    body: str
    html_url: str
    issue_url: str  # API URL of the parent issue or PR/MR
    author: str
    kind: str  # "issue" | "pr_review"


@dataclass(frozen=True)
class MentionTrigger:
    """A detected mention/assignment trigger from any forge source.

    Carries the full context so the stage handler can decide which action to
    take (implement, review, respond) without re-fetching from the API. The
    same dataclass works for GitHub (PR ``requested_reviewers[]``) and
    GitLab (MR ``reviewers[]``) because the pipeline only cares about the
    role (assignee vs reviewer) and the target object.
    """

    trigger_type: str
    # "assignee" — DEILE was assigned to an issue/PR/MR
    # "reviewer"  — DEILE was requested as reviewer on a PR/MR
    # "comment"   — @deile-one appeared in a comment
    # "body"      — @deile-one appeared in the body of an issue/PR/MR

    issue: Optional["IssueRef"] = None
    pr: Optional["PrRef"] = None
    comment: Optional["CommentRef"] = None

    @property
    def target_number(self) -> int:
        if self.issue is not None:
            return self.issue.number
        if self.pr is not None:
            return self.pr.number
        if self.comment is not None:
            # Extract trailing number from the comment URL — works for both
            # ``/issues/N`` (GH+GL), ``/pull/N`` (GH) and ``/merge_requests/N``
            # (GL). The trailing fragment (``#issuecomment-…``) is tolerated.
            m = re.search(r"/(\d+)(?:#|$)", self.comment.html_url)
            if m:
                return int(m.group(1))
        return 0

    @property
    def target_kind(self) -> str:
        """Return 'issue' or 'pr' depending on what this trigger targets.

        ``'pr'`` is used for both GitHub PRs and GitLab MRs — the pipeline
        does not distinguish them; the underlying ``ForgeClient`` knows what
        to call.
        """
        if self.pr is not None:
            return "pr"
        if self.issue is not None:
            return "issue"
        if self.comment is not None:
            return "pr" if self.comment.kind == "pr_review" else "issue"
        return "unknown"

    @property
    def dedup_key(self) -> str:
        """Return a stable deduplication key for this trigger.

        Groups triggers by the target object (issue or PR), not by the trigger
        type — so if DEILE is both assigned AND mentioned on the same issue, they
        share a dedup key and are handled together in a single dispatch.
        """
        return f"{self.target_kind}:{self.target_number}"


def compute_batch_id_for_number(kind: str, number: int) -> str:
    """SHA-8 of ``<kind>:<number>`` — collision-free unique batch lock id.

    ``kind`` is ``"issue"`` or ``"pr"``. Using the numeric id instead of the
    title avoids collisions when two issues share the same title.

    The hash is forge-agnostic by design: the same issue #42 on GitHub and
    on GitLab map to different concrete repos, but each forge maintains its
    own label namespace, so the collision space is the per-project one.
    """
    digest = hashlib.sha256(f"{kind}:{number}".encode("utf-8")).hexdigest()
    return digest[:8]


__all__ = [
    "IssueRef",
    "PrRef",
    "MrRef",
    "CommentRef",
    "MentionTrigger",
    "compute_batch_id_for_number",
]
