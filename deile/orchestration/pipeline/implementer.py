"""Pluggable implementation/review strategy for the autonomous pipeline.

The pipeline used to be hardwired to ``claude -p`` (Claude Code one-shot) for
the *implement* and *review* stages. This module introduces a strategy so the
heavy work can instead be dispatched to **another DEILE** — the long-running
``deile-worker`` Pod — over HTTP. Claude becomes one configurable option among
two, not a hard dependency.

Two strategies:

- :class:`ClaudeImplementer` — legacy path. Creates a local git worktree and
  runs ``claude -p`` inside it. Behaviour is preserved verbatim from the
  original inline code in :mod:`stages`.
- :class:`WorkerImplementer` — DEILE-to-DEILE path. POSTs a brief to the
  ``deile-worker`` control plane (:mod:`deile.infrastructure.deile_worker_client`).
  The worker clones the repo, branches, implements/reviews, runs tests and
  opens/merges the PR inside its own isolated workspace — so no local worktree
  is created on the pipeline side.

The monitor holds a single ``implementer`` (selected by
``PipelineConfig.dispatch_mode``); the stage handlers in :mod:`stages` delegate
the "do the work" step to it and keep the GitHub label orchestration to
themselves.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from deile.orchestration.pipeline.claude_dispatcher import (
    render_implement_prompt, render_mention_prompt, render_review_prompt)
from deile.orchestration.pipeline.constants import ISSUE_BODY_MAX_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.github_client import (CommentRef,
                                                            IssueRef, PrRef)
    from deile.orchestration.pipeline.monitor import PipelineMonitor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkOutcome:
    """Result of one implement/review/mention unit of work.

    ``text`` is the agent's stdout (Claude) or final summary (worker); the
    stage handler scans it for a PR URL / the word ``merged``. ``error``
    carries a short diagnostic when ``ok`` is False (surfaced to Discord).
    """

    ok: bool
    text: str
    error: str = ""


class PipelineImplementer(ABC):
    """Strategy that performs the implement / review / mention work."""

    name: str = "base"

    @abstractmethod
    async def implement(self, monitor: "PipelineMonitor", issue: "IssueRef") -> WorkOutcome:
        ...

    @abstractmethod
    async def review(self, monitor: "PipelineMonitor", pr: "PrRef") -> WorkOutcome:
        ...

    @abstractmethod
    async def mention(self, monitor: "PipelineMonitor", ref: "CommentRef") -> WorkOutcome:
        ...


# ---------------------------------------------------------------------------
# Claude Code one-shot (legacy strategy)
# ---------------------------------------------------------------------------


class ClaudeImplementer(PipelineImplementer):
    """Run ``claude -p`` inside a local git worktree (legacy default).

    Uses ``monitor.worktrees`` + ``monitor.claude`` exactly as the original
    inline stage code did, so injecting a mocked ``claude``/``worktrees`` keeps
    behaving identically.
    """

    name = "claude"

    async def implement(self, monitor: "PipelineMonitor", issue: "IssueRef") -> WorkOutcome:
        branch = monitor.branch_for_issue(issue.number)
        # Re-use an existing worktree when present; force_recreate would delete
        # and re-clone on every attempt (expensive) — reserve for /pipeline reset.
        try:
            worktree = await monitor.worktrees.create_branch_worktree(
                branch, force_recreate=False
            )
        except Exception as exc:  # noqa: BLE001 — surface as a failed outcome
            logger.exception("worktree setup for #%s failed", issue.number)
            return WorkOutcome(ok=False, text="", error=f"worktree: {type(exc).__name__}: {exc}")
        prompt = render_implement_prompt(
            monitor.config.repo, issue.number, issue.title, issue.body
        )
        result = await monitor.claude.run(prompt, cwd=worktree.path)
        return WorkOutcome(ok=result.ok, text=result.stdout, error=result.stderr.strip())

    async def review(self, monitor: "PipelineMonitor", pr: "PrRef") -> WorkOutcome:
        worktree_branch = pr.head_ref or f"pr/{pr.number}"
        try:
            wt = await monitor.worktrees.create_branch_worktree(worktree_branch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("PR worktree #%s failed", pr.number)
            return WorkOutcome(ok=False, text="", error=f"worktree: {type(exc).__name__}: {exc}")
        prompt = render_review_prompt(monitor.config.repo, pr.number, pr.title)
        result = await monitor.claude.run(prompt, cwd=wt.path)
        return WorkOutcome(ok=result.ok, text=result.stdout, error=result.stderr.strip())

    async def mention(self, monitor: "PipelineMonitor", ref: "CommentRef") -> WorkOutcome:
        prompt = render_mention_prompt(
            monitor.config.repo, ref.html_url, ref.body, ref.author
        )
        result = await monitor.claude.run(prompt, cwd=monitor.config.base_repo_path)
        return WorkOutcome(ok=result.ok, text=result.stdout, error=result.stderr.strip())


# ---------------------------------------------------------------------------
# DEILE-to-DEILE via the deile-worker (HTTP)
# ---------------------------------------------------------------------------

# Briefs are deliberately explicit and imperative: the worker DEILE owns the
# full clone → branch → implement → test → PR lifecycle inside its sandbox.
# The worker envelope already pins its CWD and forbids escaping it, so the
# briefs work strictly under ``./repo`` relative to that workspace.

_WORKER_IMPLEMENT_BRIEF = """\
Implemente a issue #{number} do repositório {repo} e abra uma Pull Request — execute de verdade, não simule nem invente.

Passo a passo:
1. Trabalhe na subpasta ./repo do seu diretório atual. Se ./repo não existir, rode: gh repo clone {repo} repo
   Se já existir, entre nela e rode: git fetch origin && git checkout {main} && git reset --hard origin/{main}
2. Dentro de ./repo, crie e dê checkout no branch {branch} a partir de {main}.
3. Implemente a feature descrita na issue abaixo. Crie/edite os arquivos necessários e ADICIONE testes cobrindo todos os casos.
4. Rode os testes e garanta 100% de aprovação. As dependências (incl. pytest) JÁ estão instaladas no ambiente — NÃO rode `pip install` (o filesystem é read-only). Para rodar só os testes novos sem esbarrar no gate global de cobertura: python3 -m pytest <arquivos_de_teste_novos> -p no:cov -q
5. Faça commit atômico e `git push -u origin {branch}`.
6. ABRA A PR (passo OBRIGATÓRIO — sem PR a tarefa NÃO está concluída):
   gh pr create --repo {repo} --base {main} --head {branch} --title "<título coerente>" --body "<resumo>. Closes #{number}."
7. CONFIRME que a PR existe antes de responder:
   gh pr view {branch} --repo {repo} --json url -q .url
   (se não retornar URL, a PR NÃO foi criada — volte ao passo 6 e crie de fato.)
8. NÃO faça force-push. NÃO altere nada fora de ./repo.
9. Na ÚLTIMA LINHA da resposta final, escreva SOMENTE a URL da PR confirmada no passo 7 (ex.: https://github.com/{repo}/pull/NN). Nada depois dela.

DEFINITION OF DONE: existe uma PR aberta cuja URL você confirmou via gh. Se push/gh/testes falharem, reporte o erro REAL — NUNCA invente uma URL nem diga "concluído" sem a PR existir.

=== Issue #{number}: {title} ===
{body}
"""

_WORKER_REVIEW_BRIEF = """\
Revise, corrija e mergeie a Pull Request #{number} do repositório {repo} — execute de verdade.

1. Garanta um clone atualizado de {repo} em ./repo (gh repo clone {repo} repo se não existir; senão git fetch origin).
2. Dentro de ./repo rode: gh pr checkout {number}
3. Rode os testes. As dependências (incl. pytest) JÁ estão no ambiente — NÃO rode `pip install` (filesystem read-only). Para os testes do PR sem o gate global de cobertura: python3 -m pytest <arquivos> -p no:cov -q. Corrija o que falhar com commits normais (SEM force-push) e dê push.
4. Quando 100% dos testes passarem, MERGEIE. NÃO tente `gh pr review --approve` (você é o autor da PR; o GitHub recusa auto-aprovação, e não há branch protection exigindo review). Mergeie via REST (evita o escopo read:org que o `gh pr merge` às vezes exige):
   gh api -X PUT repos/{repo}/pulls/{number}/merge -f merge_method=merge
   Se a REST falhar, tente: gh pr merge {number} --repo {repo} --merge
5. Confirme o merge: gh pr view {number} --repo {repo} --json state,merged -q .merged  (deve ser true).
6. Na ÚLTIMA LINHA escreva a URL da PR seguida da palavra MERGED, ex.: https://github.com/{repo}/pull/{number} MERGED
   Se NÃO conseguir mergear, escreva a URL e o motivo real — NUNCA escreva MERGED sem ter mergeado de fato.
"""

_WORKER_MENTION_BRIEF = """\
Você foi mencionado por @{author} em {context_url} (repositório {repo}).

Mensagem recebida:
{body}

Responda de forma concreta ao que foi pedido e poste a resposta como comentário usando o gh
(gh issue comment <n> --repo {repo}  ou  gh pr comment <n> --repo {repo}) apontando para {context_url}.
"""


def _render_worker_implement_brief(
    repo: str, main: str, branch: str, number: int, title: str, body: str
) -> str:
    return _WORKER_IMPLEMENT_BRIEF.format(
        repo=repo,
        main=main,
        branch=branch,
        number=number,
        title=title,
        body=(body or "").strip()[:ISSUE_BODY_MAX_CHARS] or "(sem corpo — implemente a partir do título)",
    )


def _render_worker_review_brief(repo: str, main: str, number: int, title: str) -> str:
    return _WORKER_REVIEW_BRIEF.format(repo=repo, main=main, number=number, title=title)


def _render_worker_mention_brief(repo: str, context_url: str, body: str, author: str) -> str:
    return _WORKER_MENTION_BRIEF.format(
        repo=repo, context_url=context_url, body=(body or "").strip()[:2000], author=author
    )


class WorkerImplementer(PipelineImplementer):
    """Dispatch implement/review/mention work to the ``deile-worker`` Pod.

    The worker is another DEILE running the full toolset behind an HTTP
    control plane. It clones the repo, branches, implements/reviews, runs
    tests and opens/merges the PR in its own isolated, per-channel workspace.
    The pipeline-side ``channel_id`` is synthetic (``pipeline-issue-<N>`` /
    ``pipeline-pr-<N>``) so each work item gets a stable, reusable sandbox.
    """

    name = "deile_worker"

    def __init__(self, client: Optional[object] = None) -> None:
        if client is None:
            from deile.infrastructure.deile_worker_client import \
                DeileWorkerClient
            client = DeileWorkerClient()
        self._client = client

    async def _dispatch(self, brief: str, *, channel_id: str) -> WorkOutcome:
        from deile.infrastructure.deile_worker_client import (
            WorkerDispatchError, build_dispatch_payload)

        payload = build_dispatch_payload(
            brief=brief, channel_id=channel_id, persona="developer", wait=True
        )
        try:
            data = await self._client.dispatch(payload, wait=True)
        except WorkerDispatchError as exc:
            return WorkOutcome(ok=False, text="", error=f"{exc.error_code}: {exc}"[:500])
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.exception("worker dispatch raised")
            return WorkOutcome(ok=False, text="", error=f"{type(exc).__name__}: {exc}"[:500])
        ok = bool(data.get("ok")) if isinstance(data, dict) else False
        text = str(data.get("summary") or "") if isinstance(data, dict) else ""
        if ok:
            return WorkOutcome(ok=True, text=text, error="")
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error") or data.get("summary") or "worker reported failure")
        return WorkOutcome(ok=False, text=text, error=err[:500])

    async def implement(self, monitor: "PipelineMonitor", issue: "IssueRef") -> WorkOutcome:
        branch = monitor.branch_for_issue(issue.number)
        brief = _render_worker_implement_brief(
            monitor.config.repo, monitor.config.main_branch, branch,
            issue.number, issue.title, issue.body,
        )
        return await self._dispatch(brief, channel_id=f"pipeline-issue-{issue.number}")

    async def review(self, monitor: "PipelineMonitor", pr: "PrRef") -> WorkOutcome:
        brief = _render_worker_review_brief(
            monitor.config.repo, monitor.config.main_branch, pr.number, pr.title
        )
        return await self._dispatch(brief, channel_id=f"pipeline-pr-{pr.number}")

    async def mention(self, monitor: "PipelineMonitor", ref: "CommentRef") -> WorkOutcome:
        brief = _render_worker_mention_brief(
            monitor.config.repo, ref.html_url, ref.body, ref.author
        )
        return await self._dispatch(brief, channel_id="pipeline-mentions")


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

_WORKER_ALIASES = frozenset({"deile_worker", "worker", "deile", "deile-worker"})
_CLAUDE_ALIASES = frozenset({"claude", "claude_code", "claude-code"})


def build_implementer(
    dispatch_mode: str, *, worker_client: Optional[object] = None
) -> PipelineImplementer:
    """Return the implementer strategy selected by ``dispatch_mode``.

    ``deile_worker`` (and aliases) → :class:`WorkerImplementer`;
    ``claude`` (and aliases) → :class:`ClaudeImplementer`. An unknown value
    falls back to Claude with a warning, since that is the original behaviour.
    """
    mode = (dispatch_mode or "claude").strip().lower()
    if mode in _WORKER_ALIASES:
        return WorkerImplementer(client=worker_client)
    if mode in _CLAUDE_ALIASES:
        return ClaudeImplementer()
    logger.warning("unknown pipeline dispatch_mode %r; falling back to 'claude'", dispatch_mode)
    return ClaudeImplementer()
