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
                                            MergeBlocked, WorkItemDetails,
                                            discover_cli)
from deile.orchestration.forge.refs import CommentRef, IssueRef, PrRef
from deile.orchestration.pipeline._time_utils import format_iso_utc
from deile.orchestration.pipeline.labels import (LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 MENTION_LABELS, REFINE_LABELS,
                                                 PRIORITY_LABEL_PREFIX,
                                                 PRIORITY_LABELS,
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


# Flags passed to ``gh api`` that consume the *next* argument as their value.
# Used by :func:`_rewrite_gh_api_args` to locate the endpoint positional arg.
_GH_API_VALUE_FLAGS = frozenset({
    "-X", "--method", "--jq", "--template", "--input",
})


def _rewrite_gh_api_args(host: str, prefix: str, args: tuple) -> tuple:
    """Rewrite the endpoint in a ``gh api`` args tuple to a full URL with *prefix*.

    Used for GHES deployments whose API lives at a non-default path (e.g.
    ``api/v4`` instead of the standard ``api/v3``).  ``gh`` hardcodes the
    ``/api/v3/`` prefix for GHES; this override lets the operator configure a
    different prefix via ``forge.github_api_prefix``.
    """
    rest = list(args[1:])
    skip_next = False
    for i, arg in enumerate(rest):
        if skip_next:
            skip_next = False
            continue
        if arg in _GH_API_VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if not arg.startswith(("https://", "http://")):
            rest[i] = f"https://{host}/{prefix}/{arg}"
        break
    return ("api", *rest)


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
    # Subprocess plumbing — versioned API override for GHES
    # ------------------------------------------------------------------

    async def _run(self, *args: str) -> Tuple[int, str, str]:
        """Override base _run to rewrite relative API endpoints for GHES.

        When ``forge.github_api_prefix`` is set to a non-default value (i.e.
        not ``"api"``) and the target host is not ``github.com``, ``gh api``
        would use the hardcoded ``/api/v3/`` prefix instead of the configured
        one.  This override rewrites the endpoint to a full URL so ``gh`` uses
        the operator-supplied prefix.
        """
        from deile.config.settings import get_settings
        prefix = get_settings().forge_github_api_prefix
        host = self._config.host
        if host != "github.com" and prefix != "api" and args and args[0] == "api":
            args = _rewrite_gh_api_args(host, prefix, args)
        return await super()._run(*args)

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
            # gh api --jq produces no output (and exits 1) when the PR list is
            # empty; "unexpected end of JSON input" is not a real failure.
            if "unexpected end of JSON input" in str(exc):
                return []
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

    async def get_pr_commits_since(self, number: int, since_ts: float) -> list[dict]:
        """Return commits on PR #number pushed after *since_ts* (Unix timestamp).

        Uses ``gh api repos/<r>/pulls/<n>/commits`` with a jq filter that
        extracts sha, message, date and per-file filenames. Filters
        client-side by timestamp. Returns empty list on any transport or
        parse failure (fail-open — callers treat "no commits" as "nothing
        new").
        """
        rc, out, _ = await self._run(
            "api",
            f"repos/{self.repo}/pulls/{number}/commits",
            "-q",
            (
                '[.[] | {sha: .sha, message: .commit.message, '
                'date: .commit.committer.date, '
                'files: [.files[]?.filename]}]'
            ),
        )
        if rc != 0 or not out.strip():
            return []
        try:
            all_commits = json.loads(out)
        except json.JSONDecodeError:
            logger.debug("get_pr_commits_since #%d: JSON parse failed", number)
            return []
        result: list[dict] = []
        for c in all_commits:
            ts_str = (c.get("date") or "").strip()
            try:
                iso = ts_str.replace("Z", "+00:00")
                ts = int(datetime.fromisoformat(iso).timestamp())
                if ts > since_ts:
                    result.append(c)
            except (ValueError, TypeError, OverflowError):
                continue
        return result

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

    async def get_work_item_details(
        self, kind: Literal["issue", "pr"], number: int,
    ) -> WorkItemDetails:
        """Fetch a rich snapshot for a single GitHub issue or PR.

        Makes 1–2 REST calls:
        - ``gh api repos/<r>/issues/<n>`` (both kinds — issue metadata + body)
        - ``gh pr checks`` (PR only — CI checks summary)
        """
        import re as _re

        _LINKED_RE = _re.compile(
            r"\b(?:clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|ref(?:erence(?:d|s)?)?s?)"
            r"\s+(?:![0-9]+|#(?P<issue>[0-9]+))",
            _re.IGNORECASE,
        )

        def _links(body: str) -> list:
            return [
                ("closes" if m.group(0).lower()[0] in "cfr" else "refs",
                 int(m.group("issue") or "0"))
                for m in _LINKED_RE.finditer(body or "")
                if m.group("issue")
            ]

        # ------ Item detail ------
        rc_i, out_i, _ = await self._run(
            "api", f"repos/{self.repo}/issues/{number}",
        )
        item: dict = {}
        if rc_i == 0:
            try:
                item = json.loads(out_i)
            except json.JSONDecodeError:
                pass
        if not isinstance(item, dict):
            item = {}

        author = (item.get("user") or {}).get("login", "")
        comments_count = int(item.get("comments", 0) or 0)
        body = item.get("body") or ""
        linked = _links(body)

        # PR-specific enrichment
        ci_status: Literal["passing", "failing", "pending", "none"] = "none"
        ci_summary: Tuple[int, int] = (0, 0)
        mergeability: Literal["clean", "conflict", "draft", "blocked", "unknown"] = "unknown"
        reviewers: List[Tuple[str, str]] = []

        if kind == "pr":
            # PR detail for draft/mergeable/reviewers
            rc_p, out_p, _ = await self._run(
                "api", f"repos/{self.repo}/pulls/{number}",
            )
            pr_payload: dict = {}
            if rc_p == 0:
                try:
                    pr_payload = json.loads(out_p)
                except json.JSONDecodeError:
                    pass
            if isinstance(pr_payload, dict):
                draft = bool(pr_payload.get("draft", False))
                ms = str(pr_payload.get("mergeable_state") or "unknown").lower()
                mgbl = pr_payload.get("mergeable")
                if draft:
                    mergeability = "draft"
                elif ms == "clean":
                    mergeability = "clean"
                elif ms in ("dirty", "has_hooks"):
                    mergeability = "conflict"
                elif ms == "blocked":
                    mergeability = "blocked"
                elif mgbl is False:
                    mergeability = "conflict"
                for rv in (pr_payload.get("requested_reviewers") or []):
                    login = (rv or {}).get("login", "")
                    if login:
                        reviewers.append((login, "pending"))

            # CI checks summary
            rc_c, out_c, _ = await self._run(
                "pr", "checks", str(number), "--repo", self.repo,
                "--json", "bucket,state,conclusion",
            )
            checks: list = []
            if rc_c == 0:
                try:
                    checks = json.loads(out_c) or []
                except json.JSONDecodeError:
                    pass
            if isinstance(checks, list) and checks:
                total = len(checks)
                passed = sum(1 for c in checks
                             if str((c or {}).get("bucket") or "").lower() == "pass")
                ci_summary = (passed, total)
                buckets = {str((c or {}).get("bucket") or "").lower() for c in checks}
                if "fail" in buckets:
                    ci_status = "failing"
                elif "pending" in buckets:
                    ci_status = "pending"
                elif "pass" in buckets:
                    ci_status = "passing"

        return WorkItemDetails(
            number=number,
            kind=kind,
            author=author,
            ci_status=ci_status,
            ci_checks_summary=ci_summary,
            mergeability=mergeability,
            requested_reviewers=reviewers,
            comments_count=comments_count,
            linked_items=linked,
        )

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

    async def has_bot_activity_since(
        self,
        kind: str,
        number: int,
        bot_login: str,
        *,
        since_ts: int,
    ) -> bool:
        """True se *bot_login* tem qualquer atividade no PR/issue desde
        ``since_ts`` Unix: comment, review (pra PR), merge, push de commit.

        Estratégia: query única ``gh api`` por comments + reviews em
        paralelo (gather), parseia ISO timestamps, qualquer match positivo
        retorna True. Falha de transporte → True (fail-open).
        """
        try:
            return await self._has_bot_activity_impl(
                kind, number, bot_login, since_ts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "has_bot_activity_since #%d failed (fail-open): %s",
                number, exc,
            )
            return True

    async def _has_bot_activity_impl(
        self, kind: str, number: int, bot_login: str, since_ts: int,
    ) -> bool:
        # Comments — issues + PRs compartilham endpoint.
        rc_c, out_c, _ = await self._run(
            "api", "--paginate",
            f"repos/{self.repo}/issues/{number}/comments",
            "-q", (
                f'[.[] | select(.user.login=="{bot_login}") | .created_at] | last'
            ),
        )
        if rc_c == 0 and self._iso_after(out_c, since_ts):
            return True
        if kind == "pr":
            # Reviews (formal) — só pra PRs.
            rc_r, out_r, _ = await self._run(
                "api", "--paginate",
                f"repos/{self.repo}/pulls/{number}/reviews",
                "-q", (
                    f'[.[] | select(.user.login=="{bot_login}") | .submitted_at] | last'
                ),
            )
            if rc_r == 0 and self._iso_after(out_r, since_ts):
                return True
            # Merge status — se merged depois do since_ts, conta como atividade.
            rc_m, out_m, _ = await self._run(
                "api", f"repos/{self.repo}/pulls/{number}",
                "-q", ".merged_at",
            )
            if rc_m == 0 and self._iso_after(out_m, since_ts):
                return True
            # Novo commit no branch (último commit timestamp).
            rc_p, out_p, _ = await self._run(
                "api", f"repos/{self.repo}/pulls/{number}/commits",
                "-q", "[.[].commit.committer.date] | last",
            )
            if rc_p == 0 and self._iso_after(out_p, since_ts):
                return True
        return False

    @staticmethod
    def _iso_after(out: str, since_ts: int) -> bool:
        """Helper: True se ``out`` é ISO timestamp posterior a ``since_ts``."""
        ts_str = (out or "").strip().strip('"')
        if not ts_str or ts_str == "null":
            return False
        try:
            iso = ts_str.replace("Z", "+00:00")
            return int(datetime.fromisoformat(iso).timestamp()) > since_ts
        except (ValueError, TypeError):
            return False

    async def label_applied_at(
        self, kind: str, number: int, label: str,
    ) -> Optional[int]:
        """GitHub events API → ISO timestamp do último ``labeled`` event do
        ``label`` no ``kind/number``. Suporta paginação automática via
        ``--paginate``. Retorna None se label nunca aplicada (ou erro).

        Implementação: ``gh api repos/<repo>/issues/<n>/events --paginate
        -q '...'`` filtra eventos ``event=="labeled"`` com nome batendo,
        pega o último ``created_at`` (mais recente). ISO timestamp é
        parseado pra Unix ts via ``datetime.fromisoformat`` (Python 3.11+
        suporta sufixo ``Z`` nativamente; pra anterior, normalizamos).
        """
        rc, out, err = await self._run(
            "api", "--paginate",
            f"repos/{self.repo}/issues/{number}/events",
            "-q", (
                '[.[] | select(.event=="labeled" and .label.name=='
                f'"{label}") | .created_at] | last'
            ),
        )
        if rc != 0:
            logger.debug(
                "label_applied_at #%d label=%r: gh api failed: %s",
                number, label, err[:100],
            )
            return None
        ts_str = (out or "").strip().strip('"')
        if not ts_str or ts_str == "null":
            return None
        try:
            # GitHub usa ISO 8601 com 'Z' (Python 3.11+ aceita nativamente;
            # 3.9-3.10 requer replace).
            from datetime import datetime
            iso = ts_str.replace("Z", "+00:00")
            return int(datetime.fromisoformat(iso).timestamp())
        except (ValueError, TypeError) as exc:
            logger.debug(
                "label_applied_at #%d label=%r: ts parse failed (%r): %s",
                number, label, ts_str, exc,
            )
            return None

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
            for label in (*WORKFLOW_LABELS, *REVIEW_LABELS, *MENTION_LABELS, *REFINE_LABELS, *PRIORITY_LABELS)
        ])

    # ------------------------------------------------------------------
    # Priority inheritance (issue #369)
    # ------------------------------------------------------------------

    async def inherit_priority_from_linked_issue(self, pr_number: int) -> Optional[int]:
        """Return the most urgent priority N from issues linked in the PR body.

        Parses the PR body for ``Closes #N``, ``Fixes #N``, ``Resolves #N``
        references, fetches each linked issue's labels, and returns the
        smallest N (most urgent) among ``~prioridade:N`` labels found.

        Returns ``None`` when:
        - no linked issues are found in the PR body
        - none of the linked issues carry a ``~prioridade:N`` label
        - any transport error occurs (fail-open: ``None`` is safe)
        """
        body = await self.get_pr_body(pr_number)
        if not body:
            return None
        # Match GitHub-flavoured closing keywords: Closes, Fixes, Resolves
        # (case-insensitive, with optional colon).
        linked = re.findall(
            r"\b(?:clos\w*|fix\w*|resolv\w*)\s*:?\s*#(\d+)\b",
            body, re.IGNORECASE,
        )
        if not linked:
            return None
        # Deduplicate linked issue numbers.
        unique_numbers = set(int(n) for n in linked)
        best: Optional[int] = None
        from deile.orchestration.pipeline.labels import parse_priority_from_labels
        for n in unique_numbers:
            try:
                issue = await self.get_issue(n)
            except ForgeCommandError:
                continue
            p = parse_priority_from_labels(issue.labels)
            if p is not None and (best is None or p < best):
                best = p
        return best

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
            # gh search issues uses advanced_search=true → HTTP 404 on personal
            # accounts. gh api --field sends a POST body; Search API requires
            # GET query params. Build the URL directly with urllib.parse.quote.
            login = query.lstrip("@")
            q = quote(f"mentions:{login} repo:{self.repo} state:open", safe="")
            out = await self._run_checked(
                "api", f"search/issues?q={q}&per_page={limit}",
                "--jq", (
                    ".items | map({"
                    "number, title, url: .html_url, labels, body, state,"
                    " author: {login: .user.login}"
                    "})"
                ),
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
