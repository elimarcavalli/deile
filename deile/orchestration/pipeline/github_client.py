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
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from deile.core.exceptions import DEILEError
from deile.orchestration.pipeline._time_utils import format_iso_utc
from deile.orchestration.pipeline.labels import (BATCH_LABEL_PREFIX,
                                                 LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 MENTION_LABELS, REFINE_LABELS,
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
    """Extract label names from a gh JSON item. Tolerates both the object form
    (``[{"name": ...}]``, from ``gh ... --json labels``) and the bare-string form
    (``["bug", ...]``, from some ``gh api --jq`` shapes)."""
    out: List[str] = []
    for lab in item.get("labels", []):
        if isinstance(lab, dict):
            out.append(lab["name"])
        elif isinstance(lab, str):
            out.append(lab)
    return tuple(out)


def _parse_gh_jq_output(out: Optional[str], *, log_label: str) -> List[dict]:
    """Normaliza output do ``gh api --jq`` em ``List[dict]``.

    ``gh api --jq`` muda o formato conforme o número de matches do filtro:

    - **0 matches**  → string vazia (ou apenas whitespace)
    - **1 match**    → objeto JSON puro (sem array wrapper)
    - **2+ matches** → NDJSON (objetos separados por ``\\n``, sem array wrapper)

    O ``json.loads`` direto sobre NDJSON levanta
    ``JSONDecodeError("Extra data: line 2 column 1 ...")``. Esta função
    resolve os 3 casos transparentemente. Para o caso (3) usa
    ``JSONDecoder.raw_decode`` em loop — mais robusto que ``splitlines()``
    porque tolera ``\\n`` dentro de strings JSON (que ``--jq`` não emite
    hoje, mas defensividade barata).

    Linhas/objetos malformados são logados em WARNING (com ``log_label``
    pra contexto) e pulados — política igual aos outros parsers do módulo
    (vide ``list_prs_with_review_requests``, ``search_items_mentioning``).
    """
    text = (out or "").strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    items: List[dict] = []
    idx = 0
    n = len(text)
    while idx < n:
        # Pula whitespace entre objetos (NDJSON usa `\n`, mas tolera tabs/espaços).
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            logger.warning(
                "%s: skipping malformed JSON at char %d: %s",
                log_label, idx, exc,
            )
            return items
        if isinstance(obj, list):
            # Caso array — desempacota e adiciona dicts elemento-a-elemento.
            for item in obj:
                if isinstance(item, dict):
                    items.append(item)
        elif isinstance(obj, dict):
            items.append(obj)
        # Outros tipos (string, número, bool, null) são ignorados — o filtro
        # `--jq` deste módulo só produz objetos ou arrays de objetos.
        idx = end
    return items


def _standup_item_from_gh_json(item: dict) -> dict:
    """Normalise a ``gh pr/issue list`` JSON item to the standup shape.

    Flattens ``author`` (which can be ``{"login": ...}`` or ``None``) to a
    plain string and remaps ``updatedAt`` to snake_case. Used by
    :meth:`GitHubClient.list_prs_updated_since` and
    :meth:`GitHubClient.list_issues_updated_since` — both consumed by the
    ``/standup`` slash command, which only needs basic display fields.
    """
    author = item.get("author")
    author_name = author.get("login") if isinstance(author, dict) else "?"
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "author": author_name,
        "url": item.get("url", ""),
        "updated_at": item.get("updatedAt", ""),
    }


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    url: str
    labels: Tuple[str, ...]
    body: str = ""
    state: str = "open"
    author: str = ""

    @property
    def batch_id(self) -> Optional[str]:
        return next(
            (batch_id_from_label(lb) for lb in self.labels if is_batch_label(lb)),
            None,
        )

    @classmethod
    def from_gh_json(cls, item: dict) -> "IssueRef":
        author = item.get("author") or {}
        return cls(
            number=int(item["number"]),
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            labels=_labels_from_gh(item),
            body=str(item.get("body") or ""),
            state=str(item.get("state", "open")),
            author=str(author.get("login", "")) if isinstance(author, dict) else "",
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


@dataclass(frozen=True)
class MentionTrigger:
    """A detected mention/assignment trigger from any GitHub source.

    Carries the full context so the stage handler can decide which action to
    take (implement, review, respond) without re-fetching from the API.
    """

    trigger_type: str
    # "assignee" — DEILE was assigned to an issue/PR
    # "reviewer"  — DEILE was requested as reviewer on a PR
    # "comment"   — @deile-one appeared in a comment
    # "body"      — @deile-one appeared in the body of an issue/PR

    issue: Optional["IssueRef"] = None
    pr: Optional["PrRef"] = None
    comment: Optional["CommentRef"] = None

    @property
    def target_number(self) -> int:
        """Return the issue or PR number this trigger targets."""
        if self.issue is not None:
            return self.issue.number
        if self.pr is not None:
            return self.pr.number
        if self.comment is not None:
            # Extract number from the comment's html_url or issue_url
            m = re.search(r"/(\d+)(?:#|$)", self.comment.html_url)
            if m:
                return int(m.group(1))
        return 0

    @property
    def target_kind(self) -> str:
        """Return 'issue' or 'pr' depending on what this trigger targets."""
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

    async def _list_refs(
        self, *args: str, factory: Callable[[dict], Any], log_label: Optional[str] = None
    ) -> list:
        """Run a ``gh ... list``/``api`` command and map each JSON item via ``factory``.

        Centralizes the run-checked → ``json.loads(out or "[]")`` → comprehension
        pattern shared by the list endpoints. When ``log_label`` is given a
        :class:`GhCommandError` is logged at WARNING and an empty list returned;
        otherwise it propagates (the claim/triage stages rely on that).
        """
        try:
            out = await self._run_checked(*args)
        except GhCommandError as exc:
            if log_label is None:
                raise
            logger.warning("%s failed: %s", log_label, exc)
            return []
        return [factory(item) for item in json.loads(out or "[]")]

    # -- issues -------------------------------------------------------

    async def list_issues_with_label(self, label: str, *, limit: int = 50) -> List[IssueRef]:
        """Return open issues having ``label`` (and not having any later-stage workflow label)."""
        return await self._list_refs(
            "issue", "list",
            "--repo", self.repo,
            "--state", "open",
            "--label", label,
            "--limit", str(limit),
            "--json", "number,title,url,labels,body,state,author",
            factory=IssueRef.from_gh_json,
        )

    async def get_issue(self, number: int) -> IssueRef:
        out = await self._run_checked(
            "issue", "view", str(number),
            "--repo", self.repo,
            "--json", "number,title,url,labels,body,state,author",
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
        if item.get("state", "open").lower() != "open":
            return None
        return PrRef.from_gh_json(item)

    # -- pull requests ------------------------------------------------

    async def has_open_pr_for_issue(self, number: int) -> bool:
        """True if an OPEN PR already targets/closes issue ``number`` (dedup guard).

        Issue #257: the implement stage must not open a SECOND PR for an issue that
        was already implemented through another path (e.g. a ``@deile-one`` comment
        mention firing the one-shot handler while the issue is still flowing through
        the refinement gate). Matches a PR whose body uses a closing keyword for the
        issue OR whose head branch references it — covering both the pipeline's
        ``auto/issue-N`` branches and ad-hoc branches opened via the mention path.
        Best-effort: a query failure returns False (never block work on a hiccup).
        """
        try:
            out = await self._run_checked(
                "pr", "list", "--repo", self.repo, "--state", "open",
                "--search", str(number), "--limit", "30",
                "--json", "number,body,headRefName",
            )
            prs = json.loads(out)
        except (GhCommandError, json.JSONDecodeError) as exc:
            logger.warning("has_open_pr_for_issue #%d failed: %s", number, exc)
            return False
        closes = re.compile(rf"\b(?:clos\w*|fix\w*|resolv\w*)\s+#{number}\b", re.IGNORECASE)
        needle = f"issue-{number}"
        for pr in prs:
            head = (pr.get("headRefName") or "")
            if needle in head or head.endswith(f"-{number}"):
                return True
            if closes.search(pr.get("body") or ""):
                return True
        return False

    async def list_open_prs(self, *, limit: int = 50) -> List[PrRef]:
        return await self._list_refs(
            "pr", "list",
            "--repo", self.repo,
            "--state", "open",
            "--limit", str(limit),
            "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
            factory=PrRef.from_gh_json,
        )

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

    async def assign_issue(self, number: int, login: str) -> None:
        """Assign *login* to an issue via the REST endpoint.

        Uses ``POST repos/{repo}/issues/{n}/assignees`` (needs only ``repo``
        scope) instead of ``gh issue edit --add-assignee`` — same rationale as
        :meth:`add_labels`: the ``gh`` edit path runs a GraphQL ``login`` query
        that demands ``read:org``, which the pipeline token lacks. Best-effort:
        a failure is logged, never raised (assignment is a courtesy signal).
        """
        if not login:
            return
        rc, out, err = await self._run(
            "api", "-X", "POST", f"repos/{self.repo}/issues/{number}/assignees",
            "-f", f"assignees[]={login}",
        )
        if rc != 0:
            logger.warning("assign_issue #%d -> %s failed: %s", number, login, err.strip()[:200])

    async def _transition(
        self, kind: str, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        if from_label is not None:
            await self.remove_labels(kind, number, [from_label])
        await self.add_labels(kind, number, [to_label])

    async def transition_issue(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        """Swap a workflow label on an issue (remove from_label, add to_label)."""
        await self._transition("issue", number, from_label=from_label, to_label=to_label)

    async def transition_pr(
        self, number: int, *, from_label: Optional[str], to_label: str,
    ) -> None:
        """Swap a workflow label on a PR (remove from_label, add to_label)."""
        await self._transition("pr", number, from_label=from_label, to_label=to_label)

    async def claim_with_batch(
        self,
        kind: str,
        number: int,
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

        await asyncio.gather(*[
            _create_one(label)
            for label in (*WORKFLOW_LABELS, *REVIEW_LABELS, *MENTION_LABELS, *REFINE_LABELS)
        ])

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

    # -- mention/assignment queries (issue #253) -------------------------

    async def list_issues_assigned_to(self, login: str, *, limit: int = 100) -> List["IssueRef"]:
        """Return open issues assigned to *login*."""
        return await self._list_refs(
            "issue", "list",
            "--repo", self.repo,
            "--state", "open",
            "--assignee", login,
            "--limit", str(limit),
            "--json", "number,title,url,labels,body,state,author",
            factory=IssueRef.from_gh_json,
            log_label="list_issues_assigned_to",
        )

    async def list_prs_assigned_to(self, login: str, *, limit: int = 100) -> List["PrRef"]:
        """Return open, non-draft PRs assigned to *login*."""
        return await self._list_refs(
            "pr", "list",
            "--repo", self.repo,
            "--state", "open",
            "--assignee", login,
            "--limit", str(limit),
            "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
            factory=PrRef.from_gh_json,
            log_label="list_prs_assigned_to",
        )

    async def pr_reviewer_still_requested(self, number: int, login: str) -> bool:
        """True when *login* is still in the PR's ``requested_reviewers``.

        Used by the ``review_only`` mention flow to detect a silent worker
        failure: when DEILE successfully posts a review, GitHub auto-removes it
        from ``requested_reviewers`` (natural idempotency for the reviewer
        trigger). If the worker crashes/times out before posting, the reviewer
        stays requested → the trigger re-fires next tick → infinite storm
        (#277 hit this: 20+ dispatches with zero reviews posted). The pipeline
        uses this check to apply ``~mention:processado`` as a fallback loop
        guard. Fails OPEN (returns False) so a transient gh error never
        triggers the guard spuriously.
        """
        try:
            out = await self._run_checked(
                "api", "-X", "GET",
                f"repos/{self.repo}/pulls/{number}",
                "--jq", ".requested_reviewers[].login",
            )
        except GhCommandError as exc:
            logger.warning(
                "pr_reviewer_still_requested(#%d, %s) failed: %s — assuming NOT requested (fail-open)",
                number, login, exc,
            )
            return False
        for line in (out or "").splitlines():
            if line.strip() == login:
                return True
        return False

    async def list_prs_with_review_requests(self, login: str) -> List["PrRef"]:
        """Return open PRs where *login* is a requested reviewer.

        Uses the REST API because ``gh pr list`` has no reviewer filter.
        """
        try:
            # ``-X GET`` is REQUIRED: ``gh api`` defaults to POST as soon as any
            # ``--field`` is present, so without it this POSTs to the pulls
            # endpoint — i.e. tries to CREATE a PR — and fails with HTTP 422
            # ("base"/"head" weren't supplied). Under GET the fields are query
            # params, which is what listing open PRs needs.
            out = await self._run_checked(
                "api", "-X", "GET", f"repos/{self.repo}/pulls",
                "--field", "state=open",
                "--field", "per_page=100",
                "--jq", (
                    f'.[] | select(.requested_reviewers != null) | '
                    f'select(any(.requested_reviewers[]; .login == "{login}")) | '
                    f'{{number, title, url, labels, headRefName: .head.ref, '
                    f'baseRefName: .base.ref, state, isDraft: .draft}}'
                ),
            )
        except GhCommandError as exc:
            logger.warning("list_prs_with_review_requests failed: %s", exc)
            return []
        # `gh api --jq` produz formatos diferentes conforme o número de matches:
        #   0 matches  → string vazia
        #   1 match    → objeto JSON puro (sem array wrapper)
        #   2+ matches → NDJSON (objetos separados por `\n`, sem array wrapper)
        # `json.loads()` direto quebra no caso NDJSON com:
        #   "Extra data: line 2 column 1 (char N)"
        # Helper abaixo normaliza os 3 formatos em `List[dict]`.
        items = _parse_gh_jq_output(out, log_label="list_prs_with_review_requests")
        result: List[PrRef] = []
        for item in items:
            try:
                result.append(PrRef.from_gh_json(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed review-request PR: %s", exc)
        return result

    async def search_items_mentioning(
        self, query: str, *, limit: int = 50
    ) -> tuple:
        """Return (issues, prs) where the body contains *query*.

        Uses ``gh search issues`` which covers both issues and PRs.
        """
        issues: List[IssueRef] = []
        prs: List[PrRef] = []
        try:
            out = await self._run_checked(
                "search", "issues", query,
                "--repo", self.repo,
                "--state", "open",
                "--limit", str(limit),
                "--json", "number,title,url,labels,body,state,author",
            )
        except GhCommandError as exc:
            logger.warning("search_items_mentioning failed: %s", exc)
            return issues, prs
        data = json.loads(out or "[]")
        for item in data:
            try:
                url = str(item.get("url", ""))
                if "/pull/" in url or "/pulls/" in url:
                    prs.append(PrRef.from_gh_json(item))
                else:
                    issues.append(IssueRef.from_gh_json(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed search result: %s", exc)
        return issues, prs

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
            out = await self._run_checked(
                "issue", "list",
                "--repo", self.repo,
                "--state", "open",
                "--limit", str(batch_limit),
                "--json", "number,title,url,labels,body,state,author",
            )
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

    async def list_prs_updated_since(
        self, since_iso: str, *, limit: int = 100
    ) -> List[dict]:
        """Return PRs updated since *since_iso* (ISO-8601 UTC) — any state.

        Used by ``/standup`` to enumerate recent PR activity in the window.
        Returns plain dicts (already normalised: ``author`` flattened to its
        ``login`` string, ``updated_at`` keyed in snake_case) because the
        consumer only needs basic display fields, not the full ``PrRef``
        wrapper. ``[]`` is returned on any ``gh`` failure (logged at WARNING).
        """
        return await self._list_refs(
            "pr", "list",
            "--repo", self.repo,
            "--state", "all",
            "--search", f"updated:>={since_iso}",
            "--limit", str(limit),
            "--json", "number,title,state,author,url,updatedAt",
            factory=_standup_item_from_gh_json,
            log_label="list_prs_updated_since",
        )

    async def list_issues_updated_since(
        self, since_iso: str, *, limit: int = 100
    ) -> List[dict]:
        """Return issues updated since *since_iso* (ISO-8601 UTC) — any state.

        Companion of :meth:`list_prs_updated_since` for ``/standup``. Returns
        plain dicts with the same shape; ``[]`` on any ``gh`` failure.
        """
        return await self._list_refs(
            "issue", "list",
            "--repo", self.repo,
            "--state", "all",
            "--search", f"updated:>={since_iso}",
            "--limit", str(limit),
            "--json", "number,title,state,author,url,updatedAt",
            factory=_standup_item_from_gh_json,
            log_label="list_issues_updated_since",
        )

    async def list_recently_merged_prs(self, *, limit: int = 20) -> List[PrRef]:
        """Return recently merged PRs, ordered most-recent-first.

        Used by standalone stage 4 to find PRs that need follow-up processing.
        Returns an empty list on ``gh`` error (logged at WARNING).
        """
        return await self._list_refs(
            "pr", "list",
            "--repo", self.repo,
            "--state", "merged",
            "--limit", str(limit),
            "--json", "number,title,url,labels,headRefName,baseRefName,state,isDraft",
            factory=lambda item: PrRef.from_gh_json(item, default_state="merged"),
            log_label="list_recently_merged_prs",
        )

    async def list_unclassified_prs(self) -> List[PrRef]:
        """Return open, non-draft PRs with no pipeline labels (no ``~*``).

        Candidates for automatic PR triage (Stage 0 for PRs).
        """
        prs = await self.list_open_prs()
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
            # ``-X GET`` is REQUIRED: ``gh api`` defaults to POST as soon as any
            # ``--field`` is present, so without it this POSTs to a read-only
            # comments endpoint and fails with 404. The fields become query
            # params under GET.
            out = await self._run_checked(
                "api", "-X", "GET", endpoint,
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
