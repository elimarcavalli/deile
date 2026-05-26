"""GitHub adapter — concrete :class:`ForgeClient` over the ``gh`` CLI.

Port of the legacy ``GitHubClient`` (``pipeline/github_client.py``) onto the
:class:`ForgeClient` ABC. **Every public method preserves the original
signature and contract** so the migration is byte-for-byte safe — the
pipeline tests (``test_github_client*.py``) keep passing against this class
without modification.

Two behavioural shims help during the transition:

- The constructor still accepts ``repo: str`` and ``gh_path: Optional[str]``
  for the few callers (mostly tests) that did not yet migrate to
  :class:`ForgeConfig`. When invoked that way it builds a default GH config
  in place.
- The class re-exports the legacy ``GhCommandError`` alias so ``except
  GhCommandError`` still works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Iterable, List, Literal, Optional, Tuple
from urllib.parse import quote

from deile.orchestration.forge.base import (ForgeClient, ForgeCommandError,
                                            ForgeConfig, ForgeKind,
                                            MergeBlocked, discover_cli)
from deile.orchestration.forge.refs import CommentRef, IssueRef, PrRef
from deile.orchestration.pipeline._time_utils import format_iso_utc
from deile.orchestration.pipeline.labels import (LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 MENTION_LABELS, REFINE_LABELS,
                                                 REVIEW_LABELS,
                                                 WORKFLOW_LABELS)

logger = logging.getLogger(__name__)

# GitHub username syntax — alnum or hyphen; 1-39 chars; cannot start/end with
# hyphen. Used as a defensive guard before interpolating ``login`` into a jq
# filter string (``list_prs_with_review_requests``). Anything outside this
# alphabet is rejected before it reaches the shell, eliminating jq-filter
# injection via crafted ``login`` arguments.
_GH_LOGIN_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")

# Canonical ``gh --json`` field lists. Centralised so the GH adapter never
# misses a field when one list helper diverges from another (the shape feeds
# ``IssueRef.from_gh_json`` / ``PrRef.from_gh_json``).
_ISSUE_JSON_FIELDS = "number,title,url,labels,body,state,author"
_PR_JSON_FIELDS = "number,title,url,labels,headRefName,baseRefName,state,isDraft"


# Legacy alias kept for callers that ``except GhCommandError``. Subclasses
# ForgeCommandError so the typed-error hierarchy stays clean.
class GhCommandError(ForgeCommandError):
    """Legacy alias for :class:`ForgeCommandError` raised by the GitHub path."""


def _parse_gh_jq_output(out: Optional[str], *, log_label: str) -> List[dict]:
    """Normalise ``gh api --jq`` output into ``List[dict]``.

    ``gh api --jq`` shape varies by match count:

    - 0 matches → empty string (or whitespace);
    - 1 match → bare JSON object (no array wrapper);
    - 2+ matches → NDJSON (``\\n``-separated objects, no array wrapper).

    ``json.loads`` on NDJSON raises ``"Extra data: line 2 column 1 ..."``.
    This helper handles all 3 shapes transparently using
    :class:`json.JSONDecoder.raw_decode` in a loop. Malformed segments are
    logged at WARNING and skipped (same policy as the other parsers in the
    module).
    """
    text = (out or "").strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    items: List[dict] = []
    idx = 0
    n = len(text)
    while idx < n:
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
            for item in obj:
                if isinstance(item, dict):
                    items.append(item)
        elif isinstance(obj, dict):
            items.append(obj)
        idx = end
    return items


def _standup_item_from_gh_json(item: dict) -> dict:
    """Normalise a ``gh pr/issue list`` JSON item to the standup shape.

    Flattens ``author`` (which can be ``{"login": ...}`` or ``None``) to a
    plain string and remaps ``updatedAt`` to snake_case. Used by the
    ``/standup`` slash command — it only needs basic display fields, not
    the full ``IssueRef``/``PrRef`` wrapper. Re-exported through the legacy
    shim ``pipeline/github_client.py``.
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


class GitHubForge(ForgeClient):
    """Concrete :class:`ForgeClient` over ``gh``.

    Accepts either the modern :class:`ForgeConfig` constructor signature OR
    the legacy ``(repo, *, gh_path=None)`` for backwards compatibility.
    """

    def __init__(
        self,
        config_or_repo,
        *,
        gh_path: Optional[str] = None,
    ) -> None:
        if isinstance(config_or_repo, ForgeConfig):
            if config_or_repo.kind is not ForgeKind.GITHUB:
                raise ValueError(
                    f"GitHubForge requires ForgeKind.GITHUB, got {config_or_repo.kind}"
                )
            super().__init__(config_or_repo)
        else:
            # Legacy: positional ``repo: str``. Build a default GH config.
            repo = str(config_or_repo)
            cli = gh_path or discover_cli("gh")
            config = ForgeConfig(
                kind=ForgeKind.GITHUB,
                host="github.com",
                project_path=repo,
                cli_path=cli,
            )
            super().__init__(config)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def list_issues_with_label(
        self, label: str, *, limit: int = 50,
    ) -> List[IssueRef]:
        return await self._list_refs(
            "issue", "list",
            "--repo", self.repo,
            "--state", "open",
            "--label", label,
            "--limit", str(limit),
            "--json", _ISSUE_JSON_FIELDS,
            factory=IssueRef.from_gh_json,
        )

    async def get_issue(self, number: int) -> IssueRef:
        out = await self._run_checked(
            "issue", "view", str(number),
            "--repo", self.repo,
            "--json", _ISSUE_JSON_FIELDS,
        )
        return IssueRef.from_gh_json(json.loads(out))

    async def list_issues_assigned_to(
        self, login: str, *, limit: int = 100,
    ) -> List[IssueRef]:
        return await self._list_refs(
            "issue", "list",
            "--repo", self.repo,
            "--state", "open",
            "--assignee", login,
            "--limit", str(limit),
            "--json", _ISSUE_JSON_FIELDS,
            factory=IssueRef.from_gh_json,
            log_label="list_issues_assigned_to",
        )

    async def list_unclassified_issues(self, *, limit: int = 100) -> List[IssueRef]:
        """Return open issues with no pipeline label (no ``~*``).

        Manual pagination — gh has no server-side cursor (gap #30).
        Verifica o rate-limit entre páginas e dorme se necessário.
        """
        result: List[IssueRef] = []
        seen: set = set()
        page_size = min(limit, 100)
        offset = 0
        while True:
            batch_limit = page_size + offset
            # Usa --include para capturar headers de rate-limit junto com o corpo.
            _, rl_headers = await self._api_get_json_with_headers(
                f"repos/{self.repo}/issues",
                "--field", "state=open",
                "--field", f"per_page={batch_limit}",
            )
            await self._maybe_sleep_for_rate_limit(rl_headers)

            out = await self._run_checked(
                "issue", "list",
                "--repo", self.repo,
                "--state", "open",
                "--limit", str(batch_limit),
                "--json", _ISSUE_JSON_FIELDS,
            )
            data = json.loads(out or "[]")
            for item in data:
                try:
                    issue = IssueRef.from_gh_json(item)
                    if issue.number in seen:
                        continue
                    seen.add(issue.number)
                    if any(lb.startswith("~") for lb in issue.labels):
                        continue
                    result.append(issue)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("skipping malformed issue payload: %s", exc)
                    continue
            if len(data) < batch_limit:
                break
            offset = batch_limit
            logger.debug(
                "list_unclassified_issues: fetched %d so far, extending to %d",
                len(seen), offset + page_size,
            )
        return result

    async def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Optional[List[str]] = None,
    ) -> int:
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
        except ForgeCommandError as exc:
            logger.warning("create_issue %r failed: %s", title[:60], exc)
            return 0
        m = re.search(r"/issues/(\d+)", out)
        return int(m.group(1)) if m else 0

    async def comment_on_issue(self, number: int, text: str) -> None:
        await self._run_checked(
            "issue", "comment", str(number),
            "--repo", self.repo,
            "--body", text,
        )

    async def assign_issue(self, number: int, login: str) -> None:
        """Assign *login* to an issue via the REST endpoint.

        Uses ``POST repos/{repo}/issues/{n}/assignees`` (needs only ``repo``
        scope) instead of ``gh issue edit --add-assignee`` — the ``gh`` path
        runs a GraphQL ``login`` query that demands ``read:org``, which the
        pipeline token lacks. Best-effort: a failure is logged, never raised.
        """
        if not login:
            return
        # Defesa simétrica à ``list_prs_with_review_requests``: ``login`` é
        # interpolado abaixo no valor de ``-f assignees[]=…``. Embora ``-f``
        # do gh seja form-encoded (mais seguro que jq), validamos pelo mesmo
        # alfabeto de usernames do GitHub para fechar o invariante.
        if not _GH_LOGIN_RE.fullmatch(login):
            logger.warning(
                "assign_issue #%d: login %r não é um GitHub username válido "
                "(alnum/hyphen, 1-39 chars) — rejeitando", number, login,
            )
            return
        rc, _, err = await self._run(
            "api", "-X", "POST", f"repos/{self.repo}/issues/{number}/assignees",
            "-f", f"assignees[]={login}",
        )
        if rc != 0:
            logger.warning(
                "assign_issue #%d -> %s failed: %s", number, login, err.strip()[:200]
            )

    # ------------------------------------------------------------------
    # Pull Requests
    # ------------------------------------------------------------------

    async def get_pr(self, number: int) -> Optional[PrRef]:
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", _PR_JSON_FIELDS,
            )
        except ForgeCommandError:
            return None
        item = json.loads(out)
        if item.get("state", "open").lower() != "open":
            return None
        return PrRef.from_gh_json(item)

    async def has_open_pr_for_issue(self, number: int) -> bool:
        try:
            out = await self._run_checked(
                "pr", "list", "--repo", self.repo, "--state", "open",
                "--search", str(number), "--limit", "30",
                "--json", "number,body,headRefName",
            )
            prs = json.loads(out)
        except (ForgeCommandError, json.JSONDecodeError) as exc:
            logger.warning("has_open_pr_for_issue #%d failed: %s", number, exc)
            return False
        closes = re.compile(
            rf"\b(?:clos\w*|fix\w*|resolv\w*)\s+#{number}\b", re.IGNORECASE,
        )
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
            "--json", _PR_JSON_FIELDS,
            factory=PrRef.from_gh_json,
        )

    async def list_prs_assigned_to(
        self, login: str, *, limit: int = 100,
    ) -> List[PrRef]:
        return await self._list_refs(
            "pr", "list",
            "--repo", self.repo,
            "--state", "open",
            "--assignee", login,
            "--limit", str(limit),
            "--json", _PR_JSON_FIELDS,
            factory=PrRef.from_gh_json,
            log_label="list_prs_assigned_to",
        )

    async def list_unclassified_prs(self) -> List[PrRef]:
        prs = await self.list_open_prs()
        return [
            pr for pr in prs
            if not pr.is_draft
            and not any(lb.startswith("~") for lb in pr.labels)
        ]

    async def list_recently_merged_prs(self, *, limit: int = 20) -> List[PrRef]:
        return await self._list_refs(
            "pr", "list",
            "--repo", self.repo,
            "--state", "merged",
            "--limit", str(limit),
            "--json", _PR_JSON_FIELDS,
            factory=lambda item: PrRef.from_gh_json(item, default_state="merged"),
            log_label="list_recently_merged_prs",
        )

    async def list_prs_updated_since(
        self, since_iso: str, *, limit: int = 100,
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
        self, since_iso: str, *, limit: int = 100,
    ) -> List[dict]:
        """Return issues updated since *since_iso* (ISO-8601 UTC) — any state.

        Companion of :meth:`list_prs_updated_since` for ``/standup``.
        Returns plain dicts with the same shape; ``[]`` on any ``gh`` failure.
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

    async def pr_reviewer_still_requested(self, number: int, login: str) -> bool:
        """True when *login* is still in the PR's ``requested_reviewers``.

        Fails open (returns False) so a transient gh error never triggers
        the guard spuriously — same contract as the legacy implementation.
        """
        try:
            out = await self._run_checked(
                "api", "-X", "GET",
                f"repos/{self.repo}/pulls/{number}",
                "--jq", ".requested_reviewers[].login",
            )
        except ForgeCommandError as exc:
            logger.warning(
                "pr_reviewer_still_requested(#%d, %s) failed: %s — fail-open=False",
                number, login, exc,
            )
            return False
        for line in (out or "").splitlines():
            if line.strip() == login:
                return True
        return False

    async def list_prs_with_review_requests(self, login: str) -> List[PrRef]:
        # Defesa em profundidade contra jq-filter injection: ``login`` é
        # interpolado abaixo dentro do filtro ``--jq``. ``gh api --jq`` não
        # tem equivalente ao ``--arg`` do ``jq``, então qualquer caractere
        # fora do alfabeto de usernames do GitHub é rejeitado antes de
        # alcançar o shell (loop guard fail-open mantém invariância:
        # ``[]`` em vez de raise quando a validação rejeita).
        if not _GH_LOGIN_RE.fullmatch(login or ""):
            logger.warning(
                "list_prs_with_review_requests: login %r is not a valid GitHub "
                "username (alnum/hyphen, 1-39 chars) — rejecting to prevent jq "
                "filter injection",
                login,
            )
            return []
        try:
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
        except ForgeCommandError as exc:
            logger.warning("list_prs_with_review_requests failed: %s", exc)
            return []
        items = _parse_gh_jq_output(out, log_label="list_prs_with_review_requests")
        result: List[PrRef] = []
        for item in items:
            try:
                result.append(PrRef.from_gh_json(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed review-request PR: %s", exc)
        return result

    async def comment_on_pr(self, number: int, text: str) -> None:
        await self._run_checked(
            "pr", "comment", str(number),
            "--repo", self.repo,
            "--body", text,
        )

    async def get_pr_body(self, number: int) -> str:
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", "body",
            )
            return json.loads(out).get("body", "") or ""
        except ForgeCommandError as exc:
            logger.warning("get_pr_body #%s failed: %s", number, exc)
            return ""

    async def list_pr_comments(self, number: int) -> List[str]:
        try:
            out = await self._run_checked(
                "pr", "view", str(number),
                "--repo", self.repo,
                "--json", "comments",
            )
            data = json.loads(out)
            return [c.get("body", "") for c in data.get("comments", []) if c.get("body")]
        except ForgeCommandError as exc:
            logger.warning("list_pr_comments #%s failed: %s", number, exc)
            return []

    async def set_draft(self, number: int, draft: bool) -> None:
        """Toggle the draft state of a PR. ``gh pr ready`` flips off,
        ``gh pr ready --undo`` flips on (since gh v2.40+).
        """
        if draft:
            args = ("pr", "ready", str(number), "--repo", self.repo, "--undo")
        else:
            args = ("pr", "ready", str(number), "--repo", self.repo)
        rc, _, err = await self._run(*args)
        if rc != 0:
            logger.warning(
                "set_draft #%d draft=%s failed: %s", number, draft, err.strip()[:200],
            )

    async def merge_pr(self, number: int, *, merge_method: str = "merge") -> None:
        """Merge a PR via the REST endpoint.

        Uses ``PUT /repos/<r>/pulls/<n>/merge`` to avoid the ``read:org`` scope
        the GraphQL path demands. Maps merge failures to :class:`MergeBlocked`
        so the caller gets a typed impediment.
        """
        rc, out, err = await self._run(
            "api", "-X", "PUT",
            f"repos/{self.repo}/pulls/{number}/merge",
            "-f", f"merge_method={merge_method}",
        )
        if rc == 0:
            return
        combined = f"{err}{out}".lower()
        if "405" in combined or "method not allowed" in combined or "not mergeable" in combined:
            raise MergeBlocked(
                f"GitHub refused merge of PR #{number}: {err.strip()[:200] or 'merge not allowed'}"
            )
        raise ForgeCommandError(
            ("gh", "api", "-X", "PUT", f"repos/{self.repo}/pulls/{number}/merge"),
            rc, out, err,
        )

    async def get_ci_status(
        self, number: int,
    ) -> Literal["passing", "failing", "pending", "none"]:
        """Run ``gh pr checks --json`` and collapse the result to one status.

        Usa o output JSON estruturado em vez de substring match no texto
        cru — check names como ``bypass-validator`` ou ``failure-recovery``
        casariam o substring ``fail`` antes do status real, gerando false
        positives. O campo ``bucket`` agrupa cada check em
        ``pass|fail|pending|cancel|skipping``.
        """
        rc, out, _ = await self._run(
            "pr", "checks", str(number), "--repo", self.repo,
            "--json", "bucket,state,conclusion",
        )
        if rc != 0 or not out.strip():
            return "none"
        try:
            checks = json.loads(out)
        except json.JSONDecodeError:
            return "none"
        if not isinstance(checks, list) or not checks:
            return "none"
        buckets = {str((c or {}).get("bucket") or "").lower() for c in checks}
        # Prioridade: fail > pending > pass. Buckets neutros (skip/cancel)
        # não contribuem para nenhum dos três rótulos de saída.
        if "fail" in buckets:
            return "failing"
        if "pending" in buckets:
            return "pending"
        if "pass" in buckets:
            return "passing"
        return "none"

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    async def add_labels(self, kind: str, number: int, labels: Iterable[str]) -> None:
        labels_list = list(labels)
        if not labels_list:
            return
        args = ["api", "-X", "POST", f"repos/{self.repo}/issues/{number}/labels"]
        for lb in labels_list:
            args += ["-f", f"labels[]={lb}"]
        await self._run_checked(*args)

    async def remove_labels(
        self, kind: str, number: int, labels: Iterable[str],
    ) -> None:
        labels_list = list(labels)
        if not labels_list:
            return
        for lb in labels_list:
            path = f"repos/{self.repo}/issues/{number}/labels/{quote(lb, safe='')}"
            rc, out, err = await self._run("api", "-X", "DELETE", path)
            if rc != 0:
                low = err.lower()
                if "404" in err or "not found" in low or "does not exist" in low:
                    logger.debug("remove_labels: %r absent on #%d (ignored)", lb, number)
                    continue
                raise ForgeCommandError(
                    ("gh", "api", "-X", "DELETE", path), rc, out, err,
                )

    async def _ensure_label(self, name: str, *, color: str, description: str) -> None:
        rc, _, err = await self._run(
            "label", "create", name,
            "--repo", self.repo,
            "--color", color,
            "--description", description,
        )
        if rc != 0 and "already exists" not in err.lower():
            logger.debug("ensure_label %s: rc=%d err=%s", name, rc, err.strip()[:200])

    async def ensure_pipeline_labels(self) -> None:
        async def _create_one(label: str) -> None:
            color = LABEL_COLORS.get(label, "ededed")
            description = LABEL_DESCRIPTIONS.get(label, "Pipeline-managed label")
            rc, _, _ = await self._run(
                "label", "create", label,
                "--repo", self.repo,
                "--color", color,
                "--description", description,
            )
            if rc != 0:
                logger.debug("label %s already exists or could not be created", label)

        await asyncio.gather(*[
            _create_one(label)
            for label in (*WORKFLOW_LABELS, *REVIEW_LABELS, *MENTION_LABELS, *REFINE_LABELS)
        ])

    # ------------------------------------------------------------------
    # Comments / search
    # ------------------------------------------------------------------

    async def _list_comments_since(
        self,
        endpoint: str,
        *,
        since: datetime,
        kind: str,
        url_field: str,
        log_label: str,
    ) -> List[CommentRef]:
        expected_prefix = f"repos/{self.repo}/"
        if not endpoint.startswith(expected_prefix) or ".." in endpoint:
            raise ValueError(
                f"endpoint must start with {expected_prefix!r} and contain no '..'"
            )
        since_iso = format_iso_utc(since)
        try:
            out = await self._run_checked(
                "api", "-X", "GET", endpoint,
                "--field", f"since={since_iso}",
                "--field", "per_page=100",
            )
        except ForgeCommandError as exc:
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
        return await self._list_comments_since(
            f"repos/{self.repo}/issues/comments",
            since=since,
            kind="issue",
            url_field="issue_url",
            log_label="list_issue_comments_since",
        )

    async def list_pr_review_comments_since(self, since: datetime) -> List[CommentRef]:
        return await self._list_comments_since(
            f"repos/{self.repo}/pulls/comments",
            since=since,
            kind="pr_review",
            url_field="pull_request_url",
            log_label="list_pr_review_comments_since",
        )

    async def search_items_mentioning(
        self, query: str, *, limit: int = 50,
    ) -> Tuple[List[IssueRef], List[PrRef]]:
        issues: List[IssueRef] = []
        prs: List[PrRef] = []
        try:
            out = await self._run_checked(
                "search", "issues", query,
                "--repo", self.repo,
                "--state", "open",
                "--limit", str(limit),
                "--json", _ISSUE_JSON_FIELDS,
            )
        except ForgeCommandError as exc:
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

    # ------------------------------------------------------------------
    # Repo metadata
    # ------------------------------------------------------------------

    async def default_branch(self) -> str:
        if self._config.default_branch:
            return self._config.default_branch
        try:
            out = await self._run_checked(
                "repo", "view", self.repo,
                "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name",
            )
            name = (out or "").strip() or "main"
        except ForgeCommandError as exc:
            logger.warning("default_branch lookup failed: %s — defaulting to 'main'", exc)
            name = "main"
        self._config.default_branch = name
        return name


__all__ = ["GitHubForge", "GhCommandError"]
