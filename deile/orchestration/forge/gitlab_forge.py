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
import re
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

# GitLab username regex — alphanumeric start, depois alnum/dot/underscore/hyphen
# até 255 chars. Usado como guard defensivo simétrico ao ``_GH_LOGIN_RE`` do
# adapter GitHub antes de injetar ``login`` num lookup ``users?username={login}``.
# Embora ``glab api -f`` seja form-encoded (sem risco direto de injection), a
# simetria de validação fecha o invariante de defesa em profundidade.
_GL_LOGIN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")


def _standup_item_from_gl_json(item: dict) -> dict:
    """Normalise a GitLab issue/MR payload to the standup display shape.

    GitLab vocabulary (`iid`, `web_url`, `author.username`, `state="opened"`)
    is mapped to the same canonical keys used by the GH helper, so the
    ``/standup`` slash command never branches on which forge produced the
    record. ``state="opened"`` is normalised to ``"open"`` for symmetry.
    """
    author = item.get("author") or {}
    state = str(item.get("state", ""))
    if state == "opened":
        state = "open"
    return {
        "number": item.get("iid") or item.get("number"),
        "title": item.get("title"),
        "state": state,
        "author": str(author.get("username") or author.get("name") or "?"),
        "url": str(item.get("web_url") or ""),
        "updated_at": item.get("updated_at", ""),
    }


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
            # rc=-1 por convenção "erro não-CLI" — distingue de rc=0 (sucesso)
            # e evita confundir o operador com "exit code 0 mas falhou".
            raise ForgeCommandError(
                ("glab", "api", f"projects/{self._config.encoded_project_path}"),
                -1, out, f"resposta não-JSON ao resolver project id: {exc}",
            )
        pid = payload.get("id")
        if not pid:
            # rc=-1: glab saiu 0 (OK no transporte) mas o payload não contém 'id'.
            # Pode indicar token sem permissão de leitura ou project_path errado.
            raise ForgeCommandError(
                ("glab", "api", f"projects/{self._config.encoded_project_path}"),
                -1, out, "payload do projeto não contém campo 'id' — verifique project_path e permissões do token",
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

        IMPORTANTE: forçamos ``-X GET`` SEMPRE que há parâmetros porque o
        ``glab api`` muda o método HTTP para POST por default quando recebe
        ``-f``/``--raw-field`` (doc oficial: "The default HTTP request method
        is ``GET`` if no parameters are added, and ``POST`` otherwise").
        Sem o ``-X GET`` explícito, listagens como ``GET /projects/:id/issues``
        viram POST com ``state=opened`` no body, gerando HTTP 400.
        """
        method_args = ("-X", "GET") if params else ()
        args = ("api", *method_args, endpoint, *params)
        out = await self._run_checked(*args)
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError as exc:
            # rc=-1 distingue erro de parsing (glab saiu 0) de falha de transporte.
            raise ForgeCommandError(("glab",) + args, -1, out, f"non-JSON: {exc}") from exc

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
        # ``glab issue create`` imprime a URL da nova issue; o ``iid`` é o
        # último segmento numérico do path. Em GitLab >= 17 (2025+) o output
        # pode usar ``/-/work_items/<iid>`` (sucessor unificado de issues e
        # tasks) em vez de ``/-/issues/<iid>``. Ambos compartilham o mesmo
        # ``iid`` por projeto, então tolerar os dois é suficiente.
        import re as _re
        m = _re.search(r"/(?:issues|work_items)/(\d+)", out)
        return int(m.group(1)) if m else 0

    async def comment_on_issue(self, number: int, text: str) -> None:
        # POST /projects/<id>/issues/<iid>/notes --raw-field body=<text>
        # Usa --raw-field (não -f) para evitar magic type conversion e
        # placeholder replacement do glab api: literais "null"/"true"/integers
        # e tokens `:branch`/`:user`/`:repo` seriam reinterpretados com -f,
        # corrompendo silenciosamente textos de LLM ou do operador.
        # Using REST (not ``glab issue note``) keeps the contract symmetric
        # with the GitHub adapter (REST for label mutations too) and avoids
        # the ``glab issue note --message`` interactive prompt that some
        # versions show on long messages.
        await self._run_checked(
            "api", "-X", "POST",
            f"projects/{self._project_ref}/issues/{number}/notes",
            "--raw-field", f"body={text}",
        )

    async def assign_issue(self, number: int, login: str) -> None:
        """Assign *login* to an issue.

        GitLab assignees are an array of **user IDs**, not usernames, so we
        first resolve the username via ``/users?username=<login>`` and then
        PUT the resolved id. Best-effort: failures are logged but never
        raised — assignment is a courtesy signal (mirrors the GH adapter).

        .. warning::
            A API REST do GitLab (PUT /issues/:iid) usa ``assignee_ids[]``
            como operação de **REPLACE completo** — não existe endpoint de
            "add_assignee" no v4. Isso significa que qualquer assignee
            anterior é **removido** ao chamar este método. Não faça fetch
            da lista atual para tentar merge: isso introduziria TOCTOU.
            A semântica REPLACE é intencional e documentada aqui; o
            operador deve estar ciente ao usar multi-assignee.
        """
        if not login:
            return
        # Defesa simétrica ao adapter GitHub: ``login`` é interpolado em
        # ``-f username={login}`` no lookup abaixo. Validamos contra o
        # alfabeto de usernames GitLab antes de gastar o round-trip e
        # fechamos o invariante de defesa em profundidade.
        if not _GL_LOGIN_RE.fullmatch(login):
            logger.warning(
                "assign_issue #%d: login %r não é um GitLab username válido "
                "(alnum start, alnum/dot/_/hyphen, ≤255 chars) — rejeitando",
                number, login,
            )
            return
        # GitLab PUT é semântica REPLACE — registramos em DEBUG (operacional,
        # documentado no docstring/CLAUDE.md). Anteriormente era ``warning``
        # em toda chamada, gerando ruído sob auto-routing.
        logger.debug(
            "assign_issue #%d: PUT assignee_ids[] (REPLACE; substitui qualquer "
            "assignee anterior)", number,
        )
        try:
            users = await self._api_get_json("users", "-f", f"username={login}")
        except ForgeCommandError as exc:
            logger.warning("assign_issue: user lookup %s failed: %s", login, exc)
            return
        if not isinstance(users, list) or not users:
            logger.warning("assign_issue: user %r not found", login)
            return
        user_id_raw = users[0].get("id")
        if not user_id_raw:
            logger.warning("assign_issue: user %r has no id in payload", login)
            return
        # Defesa em profundidade: GitLab REST retorna ``id`` como int, mas
        # interpolar direto na URL sem cast aceita strings arbitrárias se o
        # payload for adulterado por proxy/MITM. ``int()`` força coerção
        # numérica e falha cedo se o servidor mandar lixo.
        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            logger.warning(
                "assign_issue: user %r tem id não-numérico %r — rejeitando",
                login, user_id_raw,
            )
            return
        # IMPORTANTE: glab/GitLab rejeita ``-f assignee_ids[]=N`` para PUT —
        # o GitLab REST espera o array em query string (``?assignee_ids[]=N``),
        # não no body form-encoded que glab gera com ``-f``. Erro observado:
        # ``HTTP 400 {"error":"assignee_id, assignee_ids, ... are missing"}``.
        # Solução: encodar ``[]`` (``%5B%5D``) na URL diretamente — sem ``-f``,
        # sem ``--raw-field``. Outros parâmetros sem ``[]`` (``add_labels``,
        # ``draft``, etc.) funcionam normalmente com ``-f``.
        from urllib.parse import quote as _quote
        rc, _, err = await self._run(
            "api", "-X", "PUT",
            f"projects/{self._project_ref}/issues/{number}"
            f"?assignee_ids{_quote('[]')}={user_id}",
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

    async def list_prs_updated_since(
        self, since_iso: str, *, limit: int = 100,
    ) -> List[dict]:
        """Return MRs updated since *since_iso* (ISO-8601 UTC).

        GitLab's REST endpoint exposes ``updated_after=<iso>`` natively (full
        ISO-8601 precision, unlike the events endpoint which is day-granular).
        Normalises each payload to the standup shape used by the slash command.
        """
        try:
            items = await self._api_paginated(
                f"projects/{self._project_ref}/merge_requests",
                params=[
                    "-f", "state=all",
                    "-f", f"updated_after={since_iso}",
                    "-f", "order_by=updated_at",
                    "-f", "sort=desc",
                ],
                max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
            )
        except ForgeCommandError as exc:
            logger.warning("list_prs_updated_since failed: %s", exc)
            return []
        return [_standup_item_from_gl_json(it) for it in items[:limit]]

    async def list_issues_updated_since(
        self, since_iso: str, *, limit: int = 100,
    ) -> List[dict]:
        """Return issues updated since *since_iso* (ISO-8601 UTC)."""
        try:
            items = await self._api_paginated(
                f"projects/{self._project_ref}/issues",
                params=[
                    "-f", "state=all",
                    "-f", f"updated_after={since_iso}",
                    "-f", "order_by=updated_at",
                    "-f", "sort=desc",
                ],
                max_pages=max(1, (limit + _PER_PAGE - 1) // _PER_PAGE),
            )
        except ForgeCommandError as exc:
            logger.warning("list_issues_updated_since failed: %s", exc)
            return []
        return [_standup_item_from_gl_json(it) for it in items[:limit]]

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
        # --raw-field: evita magic type conversion e placeholder replacement
        # do glab api para conteúdo de texto livre (LLM output / input do operador).
        await self._run_checked(
            "api", "-X", "POST",
            f"projects/{self._project_ref}/merge_requests/{number}/notes",
            "--raw-field", f"body={text}",
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

        - ``detailed_merge_status`` ∈ bloqueantes → :class:`MergeBlocked` com
          mensagem específica por valor. Fallback para ``merge_status`` (campo
          legado, deprecated em GitLab v5) quando ``detailed_merge_status``
          não estiver presente (compatibilidade com instâncias antigas).
        - ``detailed_merge_status == "ci_must_pass"`` → :class:`MergeBlockedByPipeline`.
        - Valores neutros (``unchecked``, ``checking``, ``preparing``,
          ``approvals_syncing``, ``mergeable``) NÃO bloqueam o pre-check — o
          PUT dispara o cômputo final no servidor.
        - HTTP 405 "Method Not Allowed" → :class:`MergeBlocked`.

        ``merge_method`` é informativo: o GitLab decide a estratégia real
        (merge/squash/fast-forward) por projeto. ``squash=false`` mantém
        o merge plano como padrão.
        """
        # Valores de detailed_merge_status que indicam bloqueio real.
        # Fonte: https://docs.gitlab.com/ee/api/merge_requests.html#merge-status
        _DETAILED_BLOCKED: dict[str, str] = {
            "conflict": "MR tem conflito de merge",
            "not_approved": "MR requer aprovação(ões) pendente(s)",
            "not_open": "MR não está aberto",
            "requested_changes": "revisores solicitaram alterações",
            "discussions_not_resolved": "discussões não resolvidas bloqueiam o merge",
            "need_rebase": "rebase necessário antes do merge",
            "cannot_be_merged": "GitLab indica que o MR não pode ser mergeado",
        }
        # Valores neutros (unchecked/checking/preparing/approvals_syncing/
        # mergeable) NÃO bloqueiam o pre-check: o PUT dispara o cômputo final
        # no servidor. Documentados aqui para que qualquer mudança no GitLab
        # (novo valor) seja revisada antes de bloquear silenciosamente.

        # Pre-check: se o status já diz não, falha rápido com motivo claro.
        # Evita o loop "MR recusado, tenta de novo".
        try:
            mr = await self._api_get_json(
                f"projects/{self._project_ref}/merge_requests/{number}",
            )
        except ForgeCommandError as exc:
            logger.warning("merge_pr precheck #%d failed: %s", number, exc)
            mr = {}
        if isinstance(mr, dict):
            # Prefer detailed_merge_status (GitLab >= 15.6); fallback para
            # merge_status (deprecated, removido na v5 da REST API).
            dms = str(mr.get("detailed_merge_status") or "").lower()
            if dms:
                if dms == "ci_must_pass":
                    ci = await self.get_ci_status(number)
                    raise MergeBlockedByPipeline(
                        f"GitLab MR #{number}: CI deve passar antes do merge "
                        f"(detailed_merge_status=ci_must_pass, ci={ci})"
                    )
                if dms in _DETAILED_BLOCKED:
                    raise MergeBlocked(
                        f"GitLab MR #{number} detailed_merge_status={dms}: "
                        f"{_DETAILED_BLOCKED[dms]}"
                    )
                # dms é neutro (unchecked/checking/mergeable/…) — deixa o PUT tentar.
            else:
                # Fallback legado: detailed_merge_status ausente (GitLab antigo).
                ms = str(mr.get("merge_status") or "").lower()
                if ms == "cannot_be_merged":
                    raise MergeBlocked(
                        f"GitLab MR #{number} merge_status={ms}: não mergeável"
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
        labels_list = self._validate_label_names(labels)
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
        labels_list = self._validate_label_names(labels)
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

    @staticmethod
    def _validate_label_names(labels: Iterable[str]) -> List[str]:
        """Filter+validate label names antes do CSV-join em ``add/remove_labels``.

        O REST do GitLab aceita ``add_labels``/``remove_labels`` como CSV
        delimitado por vírgula. Se uma label individual contiver ``,``, o
        servidor a dividiria silenciosamente em duas labels — defesa em
        profundidade: rejeitamos qualquer label com vírgula (logando WARNING),
        em vez de corromper o destino. Vazias também são descartadas.
        """
        result: List[str] = []
        for lb in labels:
            if not lb:
                continue
            if "," in lb:
                logger.warning(
                    "label %r contém ',' — descartada (CSV add_labels/remove_labels)",
                    lb,
                )
                continue
            result.append(lb)
        return result

    async def _ensure_label(self, name: str, *, color: str, description: str) -> None:
        """Create a project label if it does not exist (idempotent).

        GitLab requires label colors to be prefixed with ``#``. The pipeline
        passes bare hex (matching GitHub's convention) so the adapter
        normalises here.
        """
        gl_color = color if color.startswith("#") else f"#{color}"
        # name e color são valores controlados pelo nosso código — -f é seguro.
        # description é texto livre (pode vir de LABEL_DESCRIPTIONS externo)
        # → --raw-field para evitar magic type conversion e placeholder replacement.
        rc, _, err = await self._run(
            "api", "-X", "POST",
            f"projects/{self._project_ref}/labels",
            "-f", f"name={name}",
            "-f", f"color={gl_color}",
            "--raw-field", f"description={description}",
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
        """Materialise a :class:`CommentRef` from a GitLab event payload.

        O segmento de URL varia conforme o tipo do noteable:
        - kind="issue"     → ``/-/issues/<iid>``
        - kind="pr_review" → ``/-/merge_requests/<iid>``
        """
        author = (note.get("author") or event.get("author") or {})
        # Reconstruct the target web URL from the event payload — GitLab events
        # do not always carry a fully-formed ``web_url`` for the note.
        target_iid = note.get("noteable_iid") or event.get("target_iid")
        if note.get("noteable_url"):
            target_web = str(note["noteable_url"])
        elif target_iid:
            # Escolhe o segmento correto conforme o tipo do comentário.
            if kind == "pr_review":
                segment = f"/-/merge_requests/{target_iid}"
            else:
                segment = f"/-/issues/{target_iid}"
            target_web = (
                f"https://{self._config.host}/{self._config.project_path}{segment}"
            )
        else:
            target_web = ""
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
