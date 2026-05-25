"""GitLab adapter — concrete :class:`ForgeClient` over the ``glab`` CLI.

Implements the same surface as :class:`GitHubForge` against GitLab's REST
API v4. Differences worth flagging up-front:

- GitLab merge requests are addressed by ``iid`` (project-internal), not
  the global numeric ``id``. The adapter always uses ``iid`` for the
  user-visible number — matching what operators see in the UI.
- Comments are GitLab **notes**. ``glab`` calls them ``note``; the REST
  endpoint is ``/notes``. Both are wrapped under the same
  ``comment_on_issue`` / ``comment_on_pr`` API as GitHub.
- A "review comment" in GitLab is a **discussion** with thread context.
  ``list_pr_review_comments_since`` flattens recent discussion notes into
  :class:`CommentRef`.
- "Issue comments since" has no project-wide endpoint in GitLab — the
  helper uses ``GET /projects/<id>/events?action=commented&after=<date>``
  which is granular to **day**; the caller's existing ``last_seen_iso``
  cursor still de-duplicates intra-day.
- Merge can be blocked by "Pipelines must succeed", protected branches or
  approval rules. The adapter maps these into typed
  :class:`MergeBlockedByPipeline` / :class:`MergeBlocked` so the pipeline
  declares ``BLOQUEADO:`` instead of retrying blindly.
- Project numeric ID is resolved on first need and cached on the
  :class:`ForgeConfig` (``config.project_id``) so subsequent REST URLs
  use the cheaper numeric form.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, List, Literal, Optional, Tuple

from deile.orchestration.forge.base import (ForgeClient, ForgeCommandError,
                                            ForgeConfig, ForgeKind,
                                            MergeBlocked,
                                            MergeBlockedByPipeline,
                                            discover_cli)
from deile.orchestration.forge.refs import CommentRef, IssueRef, PrRef
from deile.orchestration.pipeline._time_utils import format_iso_utc
from deile.orchestration.pipeline.labels import (LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 MENTION_LABELS, REFINE_LABELS,
                                                 REVIEW_LABELS,
                                                 WORKFLOW_LABELS)

logger = logging.getLogger(__name__)


# Default REST API page size — every list endpoint accepts ``per_page``.
# 100 is the hard cap GitLab enforces server-side.
_PER_PAGE = 100


class GitLabForge(ForgeClient):
    """Concrete :class:`ForgeClient` over ``glab`` + GitLab REST v4."""

    def __init__(
        self,
        config_or_path,
        *,
        glab_path: Optional[str] = None,
        host: str = "gitlab.com",
    ) -> None:
        if isinstance(config_or_path, ForgeConfig):
            if config_or_path.kind is not ForgeKind.GITLAB:
                raise ValueError(
                    f"GitLabForge requires ForgeKind.GITLAB, got {config_or_path.kind}"
                )
            super().__init__(config_or_path)
        else:
            # Legacy/tests path: positional project_path
            path = str(config_or_path)
            cli = glab_path or discover_cli("glab")
            super().__init__(ForgeConfig(
                kind=ForgeKind.GITLAB,
                host=host,
                project_path=path,
                cli_path=cli,
            ))

    # ------------------------------------------------------------------
    # REST plumbing helpers
    # ------------------------------------------------------------------

    @property
    def _project_ref(self) -> str:
        """Return the cheapest project reference (numeric id or encoded path).

        After the first call to :meth:`_resolve_project_id` the numeric id
        is cached on the :class:`ForgeConfig`; before that the URL-encoded
        path is used (one extra round-trip avoided at the cost of a longer
        URL). Either form is accepted by every GitLab REST endpoint.
        """
        return self._config.project_id or self._config.encoded_project_path

    async def _resolve_project_id(self) -> str:
        """Resolve and cache the numeric project ID.

        ``GET /projects/<encoded_path>`` returns the full project payload;
        we keep only the ``id`` field. Called lazily by methods that
        benefit from the shorter URL (label mutations, merge), but the
        adapter works without it — every REST URL accepts the encoded path
        as well.
        """
        if self._config.project_id:
            return self._config.project_id
        out = await self._run_checked(
            "api", f"projects/{self._config.encoded_project_path}",
        )
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise ForgeCommandError(
                ("glab", "api", f"projects/{self._config.encoded_project_path}"),
                0, out, f"non-JSON: {exc}",
            )
        pid = payload.get("id")
        if not pid:
            raise ForgeCommandError(
                ("glab", "api", f"projects/{self._config.encoded_project_path}"),
                0, out, "project payload missing 'id'",
            )
        self._config.project_id = str(pid)
        # Capture the default branch while we are here — it costs nothing
        # extra and saves one round-trip later.
        if not self._config.default_branch:
            self._config.default_branch = str(payload.get("default_branch") or "main")
        return self._config.project_id

    async def _api_get_json(self, endpoint: str, *params: str) -> object:
        """GET an endpoint via ``glab api`` and parse JSON.

        Caller passes additional ``-f key=value`` pairs as alternating
        ``"-f", "k=v"`` strings (matches the legacy gh shape).
        """
        args = ("api", endpoint, *params)
        out = await self._run_checked(*args)
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError as exc:
            raise ForgeCommandError(("glab",) + args, 0, out, f"non-JSON: {exc}") from exc

    async def _api_paginated(
        self,
        endpoint: str,
        *,
        params: Optional[List[str]] = None,
        max_pages: int = 50,
    ) -> List[dict]:
        """Iterate a GitLab list endpoint, returning concatenated dicts.

        ``glab api --paginate`` collects all pages and emits them as a
        single JSON array, but it depends on the ``Link`` header which not
        every glab version handles uniformly. Doing the loop here keeps
        behaviour deterministic across versions: page until the response
        is shorter than ``per_page`` (or empty), bounded by ``max_pages``
        as a safety stop.
        """
        result: List[dict] = []
        page = 1
        params = params or []
        while page <= max_pages:
            paged_params = list(params) + [
                "-f", f"per_page={_PER_PAGE}",
                "-f", f"page={page}",
            ]
            payload = await self._api_get_json(endpoint, *paged_params)
            if not isinstance(payload, list):
                # Single-object endpoints — caller shouldn't have used this helper.
                if isinstance(payload, dict):
                    result.append(payload)
                break
            for item in payload:
                if isinstance(item, dict):
                    result.append(item)
            if len(payload) < _PER_PAGE:
                break
            page += 1
        return result

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def list_issues_with_label(
        self, label: str, *, limit: int = 50,
    ) -> List[IssueRef]:
        items = await self._api_paginated(
            f"projects/{self._project_ref}/issues",
            params=[
                "-f", "state=opened",
                "-f", f"labels={label}",
            ],
            max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
        )
        return [IssueRef.from_gl_json(it) for it in items[:limit]]

    async def get_issue(self, number: int) -> IssueRef:
        payload = await self._api_get_json(
            f"projects/{self._project_ref}/issues/{number}",
        )
        if not isinstance(payload, dict):
            raise ForgeCommandError(
                ("glab", "api", f"projects/{self._project_ref}/issues/{number}"),
                0, json.dumps(payload), "expected object",
            )
        return IssueRef.from_gl_json(payload)

    async def list_issues_assigned_to(
        self, login: str, *, limit: int = 100,
    ) -> List[IssueRef]:
        try:
            items = await self._api_paginated(
                f"projects/{self._project_ref}/issues",
                params=[
                    "-f", "state=opened",
                    "-f", f"assignee_username={login}",
                ],
                max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
            )
        except ForgeCommandError as exc:
            logger.warning("list_issues_assigned_to failed: %s", exc)
            return []
        return [IssueRef.from_gl_json(it) for it in items[:limit]]

    async def list_unclassified_issues(self, *, limit: int = 100) -> List[IssueRef]:
        items = await self._api_paginated(
            f"projects/{self._project_ref}/issues",
            params=["-f", "state=opened"],
            max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
        )
        result: List[IssueRef] = []
        for it in items:
            try:
                issue = IssueRef.from_gl_json(it)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed GitLab issue: %s", exc)
                continue
            if any(lb.startswith("~") for lb in issue.labels):
                continue
            result.append(issue)
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
            "-R", self.repo,
            "-t", title,
            "-d", body,
        ]
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        try:
            out = await self._run_checked(*cmd)
        except ForgeCommandError as exc:
            logger.warning("create_issue %r failed: %s", title[:60], exc)
            return 0
        # ``glab issue create`` prints the URL of the new issue; the iid
        # is the last numeric segment.
        import re as _re
        m = _re.search(r"/issues/(\d+)", out)
        return int(m.group(1)) if m else 0

    async def comment_on_issue(self, number: int, text: str) -> None:
        # POST /projects/<id>/issues/<iid>/notes -f body=<text>
        # Using REST (not ``glab issue note``) keeps the contract symmetric
        # with the GitHub adapter (REST for label mutations too) and avoids
        # the ``glab issue note --message`` interactive prompt that some
        # versions show on long messages.
        await self._run_checked(
            "api", "-X", "POST",
            f"projects/{self._project_ref}/issues/{number}/notes",
            "-f", f"body={text}",
        )

    async def assign_issue(self, number: int, login: str) -> None:
        """Assign *login* to an issue.

        GitLab assignees are an array of **user IDs**, not usernames, so we
        first resolve the username via ``/users?username=<login>`` and then
        PUT the resolved id. Best-effort: failures are logged but never
        raised — assignment is a courtesy signal (mirrors the GH adapter).
        """
        if not login:
            return
        try:
            users = await self._api_get_json("users", "-f", f"username={login}")
        except ForgeCommandError as exc:
            logger.warning("assign_issue: user lookup %s failed: %s", login, exc)
            return
        if not isinstance(users, list) or not users:
            logger.warning("assign_issue: user %r not found", login)
            return
        user_id = users[0].get("id")
        if not user_id:
            logger.warning("assign_issue: user %r has no id in payload", login)
            return
        rc, _, err = await self._run(
            "api", "-X", "PUT",
            f"projects/{self._project_ref}/issues/{number}",
            "-f", f"assignee_ids[]={user_id}",
        )
        if rc != 0:
            logger.warning(
                "assign_issue #%d -> %s (id=%s) failed: %s",
                number, login, user_id, err.strip()[:200],
            )

    # ------------------------------------------------------------------
    # Merge requests
    # ------------------------------------------------------------------

    async def get_pr(self, number: int) -> Optional[PrRef]:
        try:
            payload = await self._api_get_json(
                f"projects/{self._project_ref}/merge_requests/{number}",
            )
        except ForgeCommandError:
            return None
        if not isinstance(payload, dict):
            return None
        # Pipeline only operates on open MRs.
        state = str(payload.get("state", "opened")).lower()
        if state not in ("opened", "open"):
            return None
        return PrRef.from_gl_json(payload)

    async def has_open_pr_for_issue(self, number: int) -> bool:
        """True if an open MR targets/closes issue ``number``.

        GitLab has a dedicated endpoint for issue → related MRs which is
        cheaper and more accurate than the GitHub fallback (search by
        text). We use it first, then back-fill with a text-search guard for
        ad-hoc MRs that may not be linked yet.
        """
        try:
            payload = await self._api_get_json(
                f"projects/{self._project_ref}/issues/{number}/related_merge_requests",
            )
        except ForgeCommandError as exc:
            logger.warning("has_open_pr_for_issue #%d failed: %s", number, exc)
            payload = []
        if isinstance(payload, list):
            for mr in payload:
                if isinstance(mr, dict) and str(mr.get("state")).lower() in (
                    "opened", "open",
                ):
                    return True
        # Back-fill: branch-name heuristic on any open MR.
        try:
            mrs = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests",
                params=["-f", "state=opened"],
                max_pages=2,
            )
        except ForgeCommandError as exc:
            logger.warning("has_open_pr_for_issue back-fill failed: %s", exc)
            return False
        needle = f"issue-{number}"
        for mr in mrs:
            head = str(mr.get("source_branch") or "")
            if needle in head or head.endswith(f"-{number}"):
                return True
        return False

    async def list_open_prs(self, *, limit: int = 50) -> List[PrRef]:
        items = await self._api_paginated(
            f"projects/{self._project_ref}/merge_requests",
            params=["-f", "state=opened"],
            max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
        )
        return [PrRef.from_gl_json(it) for it in items[:limit]]

    async def list_prs_assigned_to(
        self, login: str, *, limit: int = 100,
    ) -> List[PrRef]:
        try:
            items = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests",
                params=[
                    "-f", "state=opened",
                    "-f", f"assignee_username={login}",
                ],
                max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
            )
        except ForgeCommandError as exc:
            logger.warning("list_prs_assigned_to failed: %s", exc)
            return []
        return [PrRef.from_gl_json(it) for it in items[:limit]]

    async def list_unclassified_prs(self) -> List[PrRef]:
        prs = await self.list_open_prs()
        return [
            pr for pr in prs
            if not pr.is_draft and not any(lb.startswith("~") for lb in pr.labels)
        ]

    async def list_recently_merged_prs(self, *, limit: int = 20) -> List[PrRef]:
        try:
            items = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests",
                params=[
                    "-f", "state=merged",
                    "-f", "order_by=updated_at",
                    "-f", "sort=desc",
                ],
                max_pages=1,
            )
        except ForgeCommandError as exc:
            logger.warning("list_recently_merged_prs failed: %s", exc)
            return []
        return [PrRef.from_gl_json(it, default_state="merged") for it in items[:limit]]

    async def pr_reviewer_still_requested(self, number: int, login: str) -> bool:
        """True when *login* is in the MR's ``reviewers`` array.

        Fails open (False) — same posture as the GH adapter.
        """
        try:
            payload = await self._api_get_json(
                f"projects/{self._project_ref}/merge_requests/{number}",
            )
        except ForgeCommandError as exc:
            logger.warning(
                "pr_reviewer_still_requested(#%d, %s) failed: %s — fail-open=False",
                number, login, exc,
            )
            return False
        if not isinstance(payload, dict):
            return False
        for rev in payload.get("reviewers") or []:
            if isinstance(rev, dict) and rev.get("username") == login:
                return True
        return False

    async def list_prs_with_review_requests(self, login: str) -> List[PrRef]:
        try:
            items = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests",
                params=[
                    "-f", "state=opened",
                    "-f", f"reviewer_username={login}",
                ],
                max_pages=2,
            )
        except ForgeCommandError as exc:
            logger.warning("list_prs_with_review_requests failed: %s", exc)
            return []
        result: List[PrRef] = []
        for item in items:
            try:
                result.append(PrRef.from_gl_json(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed reviewer MR: %s", exc)
        return result

    async def comment_on_pr(self, number: int, text: str) -> None:
        await self._run_checked(
            "api", "-X", "POST",
            f"projects/{self._project_ref}/merge_requests/{number}/notes",
            "-f", f"body={text}",
        )

    async def get_pr_body(self, number: int) -> str:
        try:
            payload = await self._api_get_json(
                f"projects/{self._project_ref}/merge_requests/{number}",
            )
        except ForgeCommandError as exc:
            logger.warning("get_pr_body #%s failed: %s", number, exc)
            return ""
        if isinstance(payload, dict):
            return str(payload.get("description") or "")
        return ""

    async def list_pr_comments(self, number: int) -> List[str]:
        try:
            notes = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests/{number}/notes",
                params=[],
                max_pages=2,
            )
        except ForgeCommandError as exc:
            logger.warning("list_pr_comments #%s failed: %s", number, exc)
            return []
        # Filter out system notes (automated state changes, label updates).
        return [
            str(n.get("body") or "")
            for n in notes
            if not n.get("system") and n.get("body")
        ]

    async def set_draft(self, number: int, draft: bool) -> None:
        """Toggle the MR draft state via REST."""
        rc, _, err = await self._run(
            "api", "-X", "PUT",
            f"projects/{self._project_ref}/merge_requests/{number}",
            "-f", f"draft={'true' if draft else 'false'}",
        )
        if rc != 0:
            logger.warning(
                "set_draft #%d draft=%s failed: %s", number, draft, err.strip()[:200],
            )

    async def merge_pr(self, number: int, *, merge_method: str = "merge") -> None:
        """Merge an MR via REST.

        Maps GitLab's structured refusal modes to typed exceptions so the
        pipeline can declare ``BLOQUEADO:`` with a specific reason instead
        of retrying blindly:

        - HTTP 405 "Method Not Allowed" or ``merge_status`` ∈ {``unchecked``,
          ``cannot_be_merged``} → :class:`MergeBlocked`.
        - 405 with body mentioning "pipeline must succeed" → :class:`MergeBlockedByPipeline`.

        ``merge_method`` is informational here: GitLab decides the actual
        merge strategy per project (merge/squash/fast-forward). The flag
        ``squash=false`` keeps the default flat merge.
        """
        # Pre-check: if mergeable status already says no, fail fast with a
        # clear reason. This avoids the "MR refused, retry" loop.
        try:
            mr = await self._api_get_json(
                f"projects/{self._project_ref}/merge_requests/{number}",
            )
        except ForgeCommandError as exc:
            logger.warning("merge_pr precheck #%d failed: %s", number, exc)
            mr = {}
        if isinstance(mr, dict):
            ms = str(mr.get("merge_status") or "").lower()
            if ms in ("cannot_be_merged", "unchecked"):
                raise MergeBlocked(
                    f"GitLab MR #{number} merge_status={ms}: not mergeable yet"
                )
        squash_flag = "true" if merge_method == "squash" else "false"
        rc, out, err = await self._run(
            "api", "-X", "PUT",
            f"projects/{self._project_ref}/merge_requests/{number}/merge",
            "-f", f"squash={squash_flag}",
        )
        if rc == 0:
            return
        combined = f"{err}\n{out}".lower()
        if "pipeline" in combined and ("succeed" in combined or "must" in combined):
            # Re-fetch CI status for the error message.
            ci = await self.get_ci_status(number)
            raise MergeBlockedByPipeline(
                f"GitLab MR #{number}: 'pipeline must succeed' (current status={ci})"
            )
        if "405" in combined or "method not allowed" in combined:
            raise MergeBlocked(
                f"GitLab refused merge of MR #{number}: {err.strip()[:200] or 'method not allowed'}"
            )
        raise ForgeCommandError(
            ("glab", "api", "-X", "PUT",
             f"projects/{self._project_ref}/merge_requests/{number}/merge"),
            rc, out, err,
        )

    async def get_ci_status(
        self, number: int,
    ) -> Literal["passing", "failing", "pending", "none"]:
        """Return the latest pipeline status for the MR.

        Two REST hops: MR payload → ``head_pipeline.id`` → pipeline payload.
        Returns ``"none"`` if the MR has no associated pipeline.
        """
        try:
            mr = await self._api_get_json(
                f"projects/{self._project_ref}/merge_requests/{number}",
            )
        except ForgeCommandError as exc:
            logger.warning("get_ci_status MR fetch #%d failed: %s", number, exc)
            return "none"
        if not isinstance(mr, dict):
            return "none"
        head = mr.get("head_pipeline") or {}
        pid = head.get("id")
        if not pid:
            return "none"
        try:
            pipeline = await self._api_get_json(
                f"projects/{self._project_ref}/pipelines/{pid}",
            )
        except ForgeCommandError as exc:
            logger.warning("get_ci_status pipeline %s failed: %s", pid, exc)
            return "none"
        if not isinstance(pipeline, dict):
            return "none"
        status = str(pipeline.get("status") or "").lower()
        if status in ("success", "passed"):
            return "passing"
        if status in ("failed", "canceled"):
            return "failing"
        if status in ("pending", "running", "preparing", "waiting_for_resource", "manual", "scheduled"):
            return "pending"
        return "none"

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    async def add_labels(self, kind: str, number: int, labels: Iterable[str]) -> None:
        labels_list = [lb for lb in labels if lb]
        if not labels_list:
            return
        endpoint = self._label_target_endpoint(kind, number)
        await self._run_checked(
            "api", "-X", "PUT", endpoint,
            "-f", f"add_labels={','.join(labels_list)}",
        )

    async def remove_labels(
        self, kind: str, number: int, labels: Iterable[str],
    ) -> None:
        labels_list = [lb for lb in labels if lb]
        if not labels_list:
            return
        endpoint = self._label_target_endpoint(kind, number)
        rc, out, err = await self._run(
            "api", "-X", "PUT", endpoint,
            "-f", f"remove_labels={','.join(labels_list)}",
        )
        if rc != 0:
            low = err.lower()
            # GitLab's PUT silently ignores missing labels — but a 404 on
            # the parent issue/MR should not be raised either (idempotent).
            if "404" in err or "not found" in low:
                logger.debug(
                    "remove_labels: parent %s #%d not found (ignored)", kind, number,
                )
                return
            raise ForgeCommandError(
                ("glab", "api", "-X", "PUT", endpoint), rc, out, err,
            )

    def _label_target_endpoint(self, kind: str, number: int) -> str:
        if kind == "issue":
            return f"projects/{self._project_ref}/issues/{number}"
        if kind == "pr":
            return f"projects/{self._project_ref}/merge_requests/{number}"
        raise ValueError(f"kind must be 'issue' or 'pr', got {kind!r}")

    async def _ensure_label(self, name: str, *, color: str, description: str) -> None:
        """Create a project label if it does not exist (idempotent).

        GitLab requires label colors to be prefixed with ``#``. The pipeline
        passes bare hex (matching GitHub's convention) so the adapter
        normalises here.
        """
        gl_color = color if color.startswith("#") else f"#{color}"
        rc, _, err = await self._run(
            "api", "-X", "POST",
            f"projects/{self._project_ref}/labels",
            "-f", f"name={name}",
            "-f", f"color={gl_color}",
            "-f", f"description={description}",
        )
        if rc != 0 and "already" not in err.lower() and "has already been taken" not in err.lower():
            logger.debug("ensure_label %s: rc=%d err=%s", name, rc, err.strip()[:200])

    async def ensure_pipeline_labels(self) -> None:
        import asyncio as _asyncio

        async def _create_one(label: str) -> None:
            color = LABEL_COLORS.get(label, "ededed")
            description = LABEL_DESCRIPTIONS.get(label, "Pipeline-managed label")
            await self._ensure_label(label, color=color, description=description)

        await _asyncio.gather(*[
            _create_one(label)
            for label in (*WORKFLOW_LABELS, *REVIEW_LABELS, *MENTION_LABELS, *REFINE_LABELS)
        ])

    # ------------------------------------------------------------------
    # Comments / search (since)
    # ------------------------------------------------------------------

    async def list_issue_comments_since(self, since: datetime) -> List[CommentRef]:
        """Return notes added on issues after *since* (UTC).

        GitLab has no project-wide ``/issues/comments?since=`` endpoint, so
        this uses the events stream (``action=commented``), filtered by
        date. ``after`` is **day-granular** server-side; the caller's
        ``last_seen_iso`` cursor still de-duplicates intra-day notes via
        the post-filter on ``created_at``.
        """
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        after_date = since_utc.date().isoformat()
        try:
            events = await self._api_paginated(
                f"projects/{self._project_ref}/events",
                params=[
                    "-f", "action=commented",
                    "-f", f"after={after_date}",
                ],
                max_pages=3,
            )
        except ForgeCommandError as exc:
            logger.warning("list_issue_comments_since failed: %s", exc)
            return []
        result: List[CommentRef] = []
        for ev in events:
            note = ev.get("note") or {}
            if (note.get("noteable_type") or "").lower() != "issue":
                continue
            created_str = note.get("created_at") or ev.get("created_at") or ""
            if created_str and _is_before(created_str, since_utc):
                continue
            try:
                result.append(self._event_to_comment(ev, note, kind="issue"))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed GitLab issue note: %s", exc)
        return result

    async def list_pr_review_comments_since(self, since: datetime) -> List[CommentRef]:
        """Return MR discussion notes added after *since* (UTC).

        Discussions are GitLab's review-comment threads. The endpoint lives
        per-MR, so the helper first lists MRs updated after *since* and
        then flattens each MR's discussions whose top note falls after the
        cursor.
        """
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        try:
            mrs = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests",
                params=[
                    "-f", f"updated_after={format_iso_utc(since_utc)}",
                    "-f", "state=opened",
                ],
                max_pages=2,
            )
        except ForgeCommandError as exc:
            logger.warning("list_pr_review_comments_since list MRs failed: %s", exc)
            return []
        result: List[CommentRef] = []
        for mr in mrs:
            mr_iid = mr.get("iid")
            mr_web = mr.get("web_url") or ""
            if not mr_iid:
                continue
            try:
                discussions = await self._api_paginated(
                    f"projects/{self._project_ref}/merge_requests/{mr_iid}/discussions",
                    params=[],
                    max_pages=2,
                )
            except ForgeCommandError as exc:
                logger.warning("list discussions MR !%s failed: %s", mr_iid, exc)
                continue
            for disc in discussions:
                for note in disc.get("notes", []) or []:
                    if note.get("system"):
                        continue
                    created = note.get("created_at") or ""
                    if not created or _is_before(created, since_utc):
                        continue
                    try:
                        result.append(CommentRef(
                            comment_id=int(note["id"]),
                            body=str(note.get("body") or ""),
                            html_url=f"{mr_web}#note_{note.get('id')}",
                            issue_url=str(mr.get("web_url") or ""),
                            author=str(((note.get("author") or {}).get("username")) or ""),
                            kind="pr_review",
                        ))
                    except (KeyError, TypeError, ValueError) as exc:
                        logger.warning("skipping malformed GitLab MR note: %s", exc)
        return result

    def _event_to_comment(self, event: dict, note: dict, *, kind: str) -> CommentRef:
        """Materialise a :class:`CommentRef` from a GitLab event payload."""
        author = (note.get("author") or event.get("author") or {})
        # Reconstruct the issue web URL from the event target — GitLab events
        # do not always carry a fully-formed ``web_url`` for the note.
        target_iid = note.get("noteable_iid") or event.get("target_iid")
        target_web = note.get("noteable_url") or (
            f"https://{self._config.host}/{self._config.project_path}/-/issues/{target_iid}"
            if target_iid else ""
        )
        return CommentRef(
            comment_id=int(note.get("id") or event.get("target_id") or 0),
            body=str(note.get("body") or ""),
            html_url=f"{target_web}#note_{note.get('id')}" if note.get("id") else target_web,
            issue_url=target_web,
            author=str(author.get("username") or author.get("name") or ""),
            kind=kind,
        )

    async def search_items_mentioning(
        self, query: str, *, limit: int = 50,
    ) -> Tuple[List[IssueRef], List[PrRef]]:
        """Search issues and MRs whose body contains *query*.

        Uses the per-project search API (``/search?scope=issues|merge_requests``)
        in parallel — GitLab does not have a unified "issues+MRs" search
        like GH does.
        """
        import asyncio as _asyncio

        issues_task = self._api_paginated(
            f"projects/{self._project_ref}/search",
            params=[
                "-f", "scope=issues",
                "-f", f"search={query}",
            ],
            max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
        )
        mrs_task = self._api_paginated(
            f"projects/{self._project_ref}/search",
            params=[
                "-f", "scope=merge_requests",
                "-f", f"search={query}",
            ],
            max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
        )
        try:
            issues_raw, mrs_raw = await _asyncio.gather(issues_task, mrs_task)
        except ForgeCommandError as exc:
            logger.warning("search_items_mentioning failed: %s", exc)
            return [], []
        issues = [IssueRef.from_gl_json(it) for it in issues_raw[:limit]]
        prs = [PrRef.from_gl_json(it) for it in mrs_raw[:limit]]
        return issues, prs

    # ------------------------------------------------------------------
    # Repo metadata
    # ------------------------------------------------------------------

    async def default_branch(self) -> str:
        if self._config.default_branch:
            return self._config.default_branch
        # The project lookup also caches the default branch as a side effect.
        await self._resolve_project_id()
        return self._config.default_branch or "main"


def _is_before(iso_str: str, cursor: datetime) -> bool:
    """Return True when *iso_str* is strictly before *cursor*.

    Used by the post-filter that compensates for GitLab's day-granular
    ``after=`` parameter on the events endpoint. Treats unparseable strings
    as "after the cursor" (i.e. include them) — safer to over-deliver than
    silently drop a note.
    """
    try:
        from deile.orchestration.pipeline._time_utils import parse_iso_utc
        dt = parse_iso_utc(iso_str)
    except (ValueError, ImportError):
        return False
    return dt < cursor


__all__ = ["GitLabForge"]
