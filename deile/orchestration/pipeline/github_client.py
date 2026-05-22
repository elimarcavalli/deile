"""Async wrapper around the `gh` CLI for issue/PR/label operations.

The autonomous pipeline does not need (or want) a full GitHub-API client —
``gh`` is already authenticated locally, and the operations are simple. This
module wraps the relevant subcommands behind an async interface so the polling
loop stays non-blocking.

Each public function returns plain dicts (parsed JSON). Errors raise
:class:`GhCommandError` carrying stdout/stderr for diagnostics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from deile.core.exceptions import DEILEError
from deile.orchestration.pipeline._time_utils import format_iso_utc
from deile.orchestration.pipeline.labels import (BATCH_LABEL_PREFIX,
                                                 LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 REVIEW_LABELS,
                                                 WORKFLOW_LABELS,
                                                 batch_id_from_label,
                                                 is_batch_label,
                                                 make_batch_label)

logger = logging.getLogger(__name__)


class GhCommandError(DEILEError):
    """Raised when the `gh` CLI exits non-zero."""

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(f"gh {' '.join(cmd)} failed ({returncode}): {stderr.strip()[:300]}")
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _labels_from_gh(item: dict) -> Tuple[str, ...]:
    return tuple(
        lab["name"] for lab in item.get("labels", []) if isinstance(lab, dict)
    )


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    url: str
    labels: Tuple[str, ...]
    body: str = ""
    state: str = "open"

    @property
    def batch_id(self) -> Optional[str]:
        return next(
            (batch_id_from_label(lb) for lb in self.labels if is_batch_label(lb)),
            None,
        )

    @classmethod
    def from_gh_json(cls, item: dict) -> "IssueRef":
        return cls(
            number=int(item["number"]),
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            labels=_labels_from_gh(item),
            body=str(item.get("body") or ""),
            state=str(item.get("state", "open")),
        )


@dataclass(frozen=True)
class PrRef:
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
        return next(
            (batch_id_from_label(lb) for lb in self.labels if is_batch_label(lb)),
            None,
        )

    @classmethod
    def from_gh_json(cls, item: dict, *, default_state: str = "open") -> "PrRef":
        return cls(
            number=int(item["number"]),
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            labels=_labels_from_gh(item),
            head_ref=str(item.get("headRefName") or ""),
            base_ref=str(item.get("baseRefName") or "main"),
            state=str(item.get("state", default_state)),
            is_draft=bool(item.get("isDraft", False)),
        )


@dataclass(frozen=True)
class CommentRef:
    """A comment on an issue or PR, returned by list_*_comments_since()."""

    comment_id: int
    body: str
    html_url: str
    issue_url: str  # API URL of the parent issue or PR (issues/comments) or pull_request_url (pr review comments)
    author: str
    kind: str  # "issue" | "pr_review"


def compute_batch_id_for_number(kind: str, number: int) -> str:
    """SHA-8 of ``<kind>:<number>`` — collision-free unique batch lock id.

    ``kind`` is ``"issue"`` or ``"pr"``. Using the numeric id instead of the
    title avoids collisions when two issues share the same title.

    Migration note: existing ``~batch:`` labels created by the old
    title-based function have 8-char sha256 digests of the title. New labels
    have 8-char sha256 digests of ``"issue:N"`` or ``"pr:N"``.  They coexist
    safely because the format is identical — the pipeline only checks for the
    *presence* of a ``~batch:`` label, not the specific sha.
    """
    digest = hashlib.sha256(f"{kind}:{number}".encode("utf-8")).hexdigest()
    return digest[:8]


class GitHubClient:
    """Thin async wrapper around `gh` for the pipeline."""

    # Matches ``owner/name`` where each segment is non-empty and uses only
    # GitHub-legal characters (alnum, dot, underscore, hyphen).  Rejects
    # path-traversal sequences ('..') and any character that could escape
    # the ``repos/<repo>/`` prefix used to build REST endpoints.
    _REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

    def __init__(self, repo: str, *, gh_path: Optional[str] = None) -> None:
        # Fail-fast on path-traversal-prone shapes so the endpoint guard
        # in ``_list_comments_since`` (which checks ``startswith(repos/{repo}/)``
        # and ``".." not in endpoint``) cannot be defeated by feeding
        # ``..`` *through* ``self.repo``. The regex enforces a strict
        # ``owner/name`` shape and rejects shell metachars, whitespace,
        # extra segments, and leading/trailing ``/``.
        if "/" not in repo or ".." in repo or not self._REPO_RE.fullmatch(repo):
            raise ValueError(f"invalid repo: {repo!r}")
        self.repo = repo
        self._gh = gh_path or shutil.which("gh") or "gh"

    # -- low-level subprocess plumbing --------------------------------

    async def _run(self, *args: str, capture_stdout: bool = True) -> Tuple[int, str, str]:
        cmd = [self._gh, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE if capture_stdout else None,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return proc.returncode or 0, stdout, stderr

    async def _run_checked(self, *args: str) -> str:
        rc, out, err = await self._run(*args)
        if rc != 0:
            raise GhCommandError(args, rc, out, err)
        return out

    # -- issues -------------------------------------------------------

    async def list_issues_with_label(self, label: str, *, limit: int = 50) -> List[IssueRef]:
        """Return open issues having ``label`` (and not having any later-stage workflow label)."""
        out = await self._run_checked(
            "issue", "list",
            "--repo", self.repo,
            "--state", "open",
            "--label", label,
            "--limit", str(limit),
            "--json", "number,title,url,labels,body,state",
        )
        data = json.loads(out or "[]")
        return [IssueRef.from_gh_json(item) for item in data]

    async def get_issue(self, number: int) -> IssueRef:
        out = await self._run_checked(
            "issue", "view", str(number),
            "--repo", self.repo,
            "--json", "number,title,url,labels,body,state",
        )
        return IssueRef.from_gh_json(json.loads(out))

    async def get_pr(self, number: int) -> Optional[PrRef]:
        """Fetch a single PR by number; returns None if not found / not open."""
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
            )
        except GhCommandError:
            return None
        item = json.loads(out)
        if item.get("state", "open").lower() not in ("open",):
            return None
        return PrRef.from_gh_json(item)

    # -- pull requests ------------------------------------------------

    async def list_open_prs(self, *, limit: int = 50) -> List[PrRef]:
        out = await self._run_checked(
            "pr", "list",
            "--repo", self.repo,
            "--state", "open",
            "--limit", str(limit),
            "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
        )
        data = json.loads(out or "[]")
        return [PrRef.from_gh_json(item) for item in data]

    # -- labels -------------------------------------------------------

    async def add_labels(self, kind: str, number: int, labels: Iterable[str]) -> None:
        labels_list = list(labels)
        if not labels_list:
            return
        # Use the REST issues endpoint (PRs ARE issues in REST) instead of
        # ``gh {kind} edit --add-label``. For PRs, ``gh pr edit`` runs a
        # GraphQL query that resolves the author ``login`` and demands the
        # ``read:org`` token scope, which the pipeline token does not carry —
        # so labeling a PR fails. The REST call needs only ``repo`` scope and
        # behaves identically for issues and PRs.
        args = ["api", "-X", "POST", f"repos/{self.repo}/issues/{number}/labels"]
        for lb in labels_list:
            args += ["-f", f"labels[]={lb}"]
        await self._run_checked(*args)

    async def remove_labels(self, kind: str, number: int, labels: Iterable[str]) -> None:
        labels_list = list(labels)
        if not labels_list:
            return
        # REST DELETE per label (see add_labels for why we avoid gh pr edit).
        # The label name lives in the URL path, so it must be percent-encoded
        # (``~`` and ``:`` in workflow/batch labels are reserved). A 404 means
        # the label was not present — treat that as an idempotent no-op so a
        # transition whose ``from_label`` is already absent doesn't error.
        for lb in labels_list:
            path = f"repos/{self.repo}/issues/{number}/labels/{quote(lb, safe='')}"
            rc, out, err = await self._run("api", "-X", "DELETE", path)
            if rc != 0:
                low = err.lower()
                if "404" in err or "not found" in low or "does not exist" in low:
                    logger.debug("remove_labels: %r absent on #%d (ignored)", lb, number)
                    continue
                raise GhCommandError(("api", "-X", "DELETE", path), rc, out, err)

    async def transition_issue(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        """Swap a workflow label on an issue (remove from_label, add to_label)."""
        if from_label is not None:
            await self.remove_labels("issue", number, [from_label])
        await self.add_labels("issue", number, [to_label])

    async def transition_pr(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        """Swap a workflow label on a PR (remove from_label, add to_label)."""
        if from_label is not None:
            await self.remove_labels("pr", number, [from_label])
        await self.add_labels("pr", number, [to_label])

    async def claim_with_batch(
        self,
        kind: str,
        number: int,
        title: str,
    ) -> Optional[str]:
        """Try to claim an issue/PR by attaching a batch lock label.

        Returns the batch_id on success, or None if the issue already has a
        ``~batch:`` label (someone else picked it up).

        Best-effort TOCTOU mitigation: after ``add_labels`` we re-fetch the
        item and verify that only OUR batch label is present.  If another
        ``~batch:`` label appeared between our read and our write we remove the
        label we just added and yield to the winner.  This is not a true
        distributed lock (no ``If-Match``/ETag support in ``gh``), but it
        catches the common case where two monitors overlap on a fast repo.
        """
        if kind not in ("issue", "pr"):
            raise ValueError(f"kind must be 'issue' or 'pr', got {kind!r}")

        async def _fetch_current():
            if kind == "issue":
                return await self.get_issue(number)
            return await self.get_pr(number)  # may be None

        current = await _fetch_current()
        if current is None:
            return None
        if current.batch_id is not None:
            return None

        batch_id = compute_batch_id_for_number(kind, number)
        label = make_batch_label(batch_id)
        await self._ensure_label(label, color="d73a4a", description="Pipeline batch lock")
        await self.add_labels(kind, number, [label])

        # Re-fetch to verify we are the sole claimant.
        after = await _fetch_current()
        if after is None:
            return None
        foreign = [
            lb for lb in after.labels
            if is_batch_label(lb) and lb != label
        ]
        if foreign:
            # Another monitor also applied a batch label — we lost the race.
            # Remove ours and yield.
            logger.warning(
                "claim_with_batch: TOCTOU race detected on %s #%d; "
                "foreign labels=%s; removing our label and yielding",
                kind, number, foreign,
            )
            try:
                await self.remove_labels(kind, number, [label])
            except GhCommandError as exc:
                logger.warning(
                    "claim_with_batch: could not remove our label after race: %s", exc
                )
            return None

        return batch_id

    async def _ensure_label(self, name: str, *, color: str, description: str) -> None:
        """Create label if it doesn't exist; ignore "already exists" errors."""
        rc, _, err = await self._run(
            "label", "create", name,
            "--repo", self.repo,
            "--color", color,
            "--description", description,
        )
        if rc != 0 and "already exists" not in err.lower():
            logger.debug("ensure_label %s: rc=%d err=%s", name, rc, err.strip()[:200])

    async def ensure_pipeline_labels(self) -> None:
        """Create all pipeline-managed labels on the repo if they don't exist."""
        async def _create_one(label: str) -> None:
            color = LABEL_COLORS.get(label, "ededed")
            description = LABEL_DESCRIPTIONS.get(label, "Pipeline-managed label")
            rc, _, _ = await self._run(
                "label", "create", label,
                "--repo", self.repo,
                "--color", color,
                "--description", description,
            )
            # rc != 0 typically means "already exists"; we ignore that case.
            if rc != 0:
                logger.debug("label %s already exists or could not be created", label)

        await asyncio.gather(*[_create_one(label) for label in (*WORKFLOW_LABELS, *REVIEW_LABELS)])

    async def comment_on_issue(self, number: int, text: str) -> None:
        await self._run_checked(
            "issue", "comment", str(number),
            "--repo", self.repo,
            "--body", text,
        )

    async def comment_on_pr(self, number: int, text: str) -> None:
        await self._run_checked(
            "pr", "comment", str(number),
            "--repo", self.repo,
            "--body", text,
        )

    async def get_pr_body(self, number: int) -> str:
        """Fetch the body of any PR (open or merged) by number."""
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", "body",
            )
            return json.loads(out).get("body", "") or ""
        except GhCommandError as exc:
            logger.warning("get_pr_body #%s failed: %s", number, exc)
            return ""

    async def list_pr_comments(self, number: int) -> List[str]:
        """Return the body text of every general comment on a PR."""
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", "comments",
            )
            data = json.loads(out)
            return [c.get("body", "") for c in data.get("comments", []) if c.get("body")]
        except GhCommandError as exc:
            logger.warning("list_pr_comments #%s failed: %s", number, exc)
            return []

    async def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Optional[List[str]] = None,
    ) -> int:
        """Create a new issue and return its number (0 on failure)."""
        cmd = [
            "issue", "create",
            "--repo", self.repo,
            "--title", title,
            "--body", body,
        ]
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        try:
            out = await self._run_checked(*cmd)
        except GhCommandError as exc:
            logger.warning("create_issue %r failed: %s", title[:60], exc)
            return 0
        m = re.search(r"/issues/(\d+)", out)
        return int(m.group(1)) if m else 0

    async def list_unclassified_issues(self, *, limit: int = 100) -> List[IssueRef]:
        """Return open issues that have no pipeline labels (no ``~workflow:*``, ``~batch:*``, ``~review:*``).

        These are candidates for Stage 0 auto-classification.  The ``gh``
        CLI does not expose a server-side cursor, so we fetch in batches of
        *limit* and keep going while the page is full (gap #30).
        """
        result: List[IssueRef] = []
        seen: set = set()
        page_size = min(limit, 100)
        # gh issue list has no pagination cursor; we approximate by bumping
        # --limit until we get fewer results than we asked for.
        offset = 0
        while True:
            batch_limit = page_size + offset
            try:
                out = await self._run_checked(
                    "issue", "list",
                    "--repo", self.repo,
                    "--state", "open",
                    "--limit", str(batch_limit),
                    "--json", "number,title,url,labels,body,state",
                )
            except GhCommandError:
                raise
            data = json.loads(out or "[]")
            new_items = 0
            for item in data:
                try:
                    issue = IssueRef.from_gh_json(item)
                    if issue.number in seen:
                        continue
                    seen.add(issue.number)
                    if any(lb.startswith("~") for lb in issue.labels):
                        continue
                    result.append(issue)
                    new_items += 1
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("skipping malformed issue payload: %s", exc)
                    continue
            # If gh returned fewer items than we asked for, we've exhausted the list.
            if len(data) < batch_limit:
                break
            # Otherwise, bump offset and try again to pick up the next page.
            offset = batch_limit
            logger.debug(
                "list_unclassified_issues: fetched %d total so far, extending to %d",
                len(seen), offset + page_size,
            )
        return result

    async def list_recently_merged_prs(self, *, limit: int = 20) -> List[PrRef]:
        """Return recently merged PRs, ordered most-recent-first.

        Used by standalone stage 4 to find PRs that need follow-up processing.
        Returns an empty list on ``gh`` error (logged at WARNING).
        """
        try:
            out = await self._run_checked(
                "pr", "list",
                "--repo", self.repo,
                "--state", "merged",
                "--limit", str(limit),
                "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
            )
        except GhCommandError as exc:
            logger.warning("list_recently_merged_prs failed: %s", exc)
            return []
        data = json.loads(out or "[]")
        return [PrRef.from_gh_json(item, default_state="merged") for item in data]

    async def list_unclassified_prs(self) -> List[PrRef]:
        """Return open, non-draft PRs with no pipeline labels (no ``~*``).

        Candidates for automatic PR triage (Stage 0 for PRs).
        """
        try:
            prs = await self.list_open_prs()
        except GhCommandError:
            raise
        return [
            pr for pr in prs
            if not pr.is_draft
            and not any(lb.startswith("~") for lb in pr.labels)
        ]

    async def _list_comments_since(
        self,
        endpoint: str,
        *,
        since: datetime,
        kind: str,
        url_field: str,
        log_label: str,
    ) -> List[CommentRef]:
        """Shared implementation for list_issue_comments_since / list_pr_review_comments_since."""
        # Defence-in-depth: every current caller builds ``endpoint`` from
        # ``self.repo``; assert here so a future caller cannot pass an
        # attacker-influenced REST path through to ``gh api``.
        expected_prefix = f"repos/{self.repo}/"
        if not endpoint.startswith(expected_prefix) or ".." in endpoint:
            raise ValueError(
                f"endpoint must start with {expected_prefix!r} and contain no '..'"
            )
        since_iso = format_iso_utc(since)
        try:
            out = await self._run_checked(
                "api", endpoint,
                "--field", f"since={since_iso}",
                "--field", "per_page=100",
            )
        except GhCommandError as exc:
            logger.warning("%s failed: %s", log_label, exc)
            return []
        data = json.loads(out or "[]")
        result: List[CommentRef] = []
        for item in data:
            try:
                result.append(CommentRef(
                    comment_id=int(item["id"]),
                    body=str(item.get("body") or ""),
                    html_url=str(item.get("html_url", "")),
                    issue_url=str(item.get(url_field, "")),
                    author=str((item.get("user") or {}).get("login", "")),
                    kind=kind,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed %s comment payload: %s", kind, exc)
        return result

    async def list_issue_comments_since(self, since: datetime) -> List[CommentRef]:
        """Return all issue comments posted after *since* (UTC).

        Uses the GitHub REST API via ``gh api``. Returns empty list on error.
        """
        return await self._list_comments_since(
            f"repos/{self.repo}/issues/comments",
            since=since,
            kind="issue",
            url_field="issue_url",
            log_label="list_issue_comments_since",
        )

    async def list_pr_review_comments_since(self, since: datetime) -> List[CommentRef]:
        """Return all PR review comments posted after *since* (UTC).

        Uses the GitHub REST API via ``gh api``. Returns empty list on error.
        """
        return await self._list_comments_since(
            f"repos/{self.repo}/pulls/comments",
            since=since,
            kind="pr_review",
            url_field="pull_request_url",
            log_label="list_pr_review_comments_since",
        )

    async def clear_batch_label(self, kind: str, number: int) -> None:
        """Remove all ``~batch:*`` labels from an issue or PR (gap #9).

        Called after stage 3 concludes to prevent orphaned lock labels.
        Best-effort: errors are logged at WARNING but not re-raised.
        """
        if kind == "issue":
            try:
                current = await self.get_issue(number)
            except GhCommandError as exc:
                logger.warning("clear_batch_label: could not fetch %s #%d: %s", kind, number, exc)
                return
        elif kind == "pr":
            current = await self.get_pr(number)
            if current is None:
                return
        else:
            raise ValueError(f"kind must be 'issue' or 'pr', got {kind!r}")

        batch_labels = [lb for lb in current.labels if lb.startswith(BATCH_LABEL_PREFIX)]
        if not batch_labels:
            return
        try:
            await self.remove_labels(kind, number, batch_labels)
            logger.debug("cleared batch labels %s from %s #%d", batch_labels, kind, number)
        except GhCommandError as exc:
            logger.warning("clear_batch_label: remove failed for %s #%d: %s", kind, number, exc)
