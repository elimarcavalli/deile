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
    render_implement_prompt, render_review_prompt)
from deile.orchestration.pipeline.constants import ISSUE_BODY_MAX_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.github_client import (CommentRef,
                                                            IssueRef,
                                                            MentionTrigger,
                                                            PrRef)
    from deile.orchestration.pipeline.monitor import PipelineMonitor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkOutcome:
    """Result of one implement/review/mention unit of work.

    ``text`` is the agent's stdout (Claude) or final summary (worker); the
    stage handler scans it for a PR URL / the word ``merged``. ``error``
    carries a short diagnostic when ``ok`` is False (surfaced to Discord).

    Resume fields (issue #254) are populated only on the deile-worker path
    when a ``resume`` context was sent; they carry the worker's GROUND-TRUTH
    structured result so the stage handler can decide concluido/incompleto/
    bloqueado without trusting the model's output format:

    - ``ended`` — ``"concluido"`` | ``"incompleto"`` | ``"bloqueado"`` | ``""``
      (empty when the worker returned no structured block, e.g. Claude path).
    - ``pr_url`` — confirmed PR URL the worker saw (may be empty).
    - ``motivo_bloqueio`` — agent-declared ``BLOQUEADO:`` reason (when blocked).
    - ``motivo_fim_loop`` — how the tool-loop ended (timeout/cap/natural/erro).
    - ``fingerprint`` — substantive-change hash for the progress guard.
    - ``tentativa`` — 1-based attempt counter persisted in the workspace.
    - ``budget_acumulado_s`` — accumulated wall-clock across attempts (ceiling).
    """

    ok: bool
    text: str
    error: str = ""
    ended: str = ""
    pr_url: str = ""
    motivo_bloqueio: str = ""
    motivo_fim_loop: str = ""
    fingerprint: str = ""
    tentativa: int = 0
    budget_acumulado_s: float = 0.0


class PipelineImplementer(ABC):
    """Strategy that performs the implement / review / mention work."""

    name: str = "base"

    @abstractmethod
    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        ...

    @abstractmethod
    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        ...

    @abstractmethod
    async def mention(
        self,
        monitor: "PipelineMonitor",
        ref: "MentionTrigger",
        *,
        trigger_types: list[str] | None = None,
        all_triggers: list["MentionTrigger"] | None = None,
        mode: str = "comment",
        resume: bool = False,
    ) -> WorkOutcome:
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

    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        # ``resume`` is accepted for interface parity. The Claude path already
        # reuses an existing worktree (``force_recreate=False``) so partial work
        # in the worktree survives between attempts; it has no structured
        # ground-truth contract (that lives in the deile-worker path), so the
        # flag does not change behaviour here beyond the existing reuse.
        branch = monitor.branch_for_issue(issue.number)
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

    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        worktree_branch = pr.head_ref or f"pr/{pr.number}"
        try:
            wt = await monitor.worktrees.create_branch_worktree(worktree_branch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("PR worktree #%s failed", pr.number)
            return WorkOutcome(ok=False, text="", error=f"worktree: {type(exc).__name__}: {exc}")
        prompt = render_review_prompt(monitor.config.repo, pr.number, pr.title)
        result = await monitor.claude.run(prompt, cwd=wt.path)
        return WorkOutcome(ok=result.ok, text=result.stdout, error=result.stderr.strip())

    async def mention(
        self,
        monitor: "PipelineMonitor",
        ref: "MentionTrigger",
        *,
        trigger_types: list[str] | None = None,
        all_triggers: list["MentionTrigger"] | None = None,
        mode: str = "comment",
        resume: bool = False,
    ) -> WorkOutcome:
        # ``mode``/``resume`` are accepted for interface parity with the worker
        # path; the legacy Claude path keeps its single context-aware prompt.
        prompt = _render_claude_mention_prompt(
            monitor.config.repo, ref, trigger_types or [], all_triggers or []
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
Você é o QUALITY GATE final da Pull Request #{number} do repositório {repo}. Revise com RIGOR, corrija e — só se passar no portão — mergeie. Execute de verdade: testes verdes NÃO bastam.

1. Garanta um clone atualizado de {repo} em ./repo (gh repo clone {repo} repo se não existir; senão git fetch origin). Dentro de ./repo: gh pr checkout {number}
2. LEIA O DIFF INTEIRO e entenda a intenção da mudança: git diff {main}...HEAD ; git diff HEAD. Liste os arquivos tocados e leia cada um por completo.
3. AVALIE contra o checklist do revisor e anote cada achado (arquivo:linha + problema):
   - Corretude e IDEMPOTÊNCIA: a lógica re-executa sem efeito duplicado? Algo em loop/por tick/agendado re-dispara a cada execução sem claim/dedup/cursor? (storms de processamento duplicado são a classe de bug nº 1 deste projeto)
   - SOLID / SRP / DRY / KISS: responsabilidade única; sem duplicação real nem abstração prematura.
   - Arquitetura hexagonal: núcleo sem SDK externo; componentes via registry; Tool retorna ToolResult; I/O async.
   - SEGURANÇA: input sanitizado antes de shell/SQL/fs; sem segredo em log; sem injeção (nada de f-string em filtro jq/shell/SQL — use --arg/binding).
   - Error handling: sem bare except; exceções tipadas (DEILEError); CancelledError re-raised; nenhum awaitable sem await.
   - Testes cobrem casos de BORDA e a regressão que a PR alega corrigir.
   - Packaging/deploy: arquivo novo importado em runtime está no COPY do Dockerfile E no allowlist do .dockerignore?
4. CORRIJA os achados com commits normais (SEM force-push) e dê push. Adicione os testes que faltarem.
5. Rode os testes e garanta 100% de aprovação. Dependências (incl. pytest) JÁ instaladas — NÃO rode `pip install` (filesystem read-only). Testes do PR sem o gate global de cobertura: python3 -m pytest <arquivos> -p no:cov -q
5b. THREADS/NOTAS de review pendentes: liste os comentários de review (gh api repos/{repo}/pulls/{number}/comments). Para CADA thread/nota, JULGUE criticamente se o que foi pedido está realmente correto — NÃO obedeça cegamente. Se procede, RESOLVA (faça a mudança + responda a thread citando o commit). Se NÃO procede, responda a thread com uma JUSTIFICATIVA concreta de por que não fazer. Não deixe thread pendente sem ação ou justificativa.
6. DOCUMENTE as evidências como comentário na PR (gh pr comment {number} --repo {repo} --body "..."): o que revisou, os achados, as correções, como tratou cada thread, e a saída REAL dos testes.
7. VEREDITO:
   - Checklist OK E testes verdes → MERGEIE. Você é o autor da PR; NÃO use `gh pr review --approve` (o GitHub recusa auto-aprovação). Mergeie via REST (evita o escopo read:org): gh api -X PUT repos/{repo}/pulls/{number}/merge -f merge_method=merge (fallback: gh pr merge {number} --repo {repo} --merge). Confirme: gh pr view {number} --repo {repo} --json merged -q .merged (deve ser true).
   - Impedimento REAL que você não pode resolver com segurança (decisão de produto pendente, falta credencial/segredo, mudança quebraria contrato sem migração) → NÃO mergeie; comente o motivo na PR e escreva numa linha começando com `BLOQUEADO: <motivo concreto>`.
8. Na ÚLTIMA LINHA: a URL da PR seguida de MERGED (ex.: https://github.com/{repo}/pull/{number} MERGED) se mergeou; OU a linha `BLOQUEADO: <motivo>`. NUNCA escreva MERGED sem ter mergeado de fato; NUNCA invente resultado.
"""

# --- Resume briefs (issue #254) -----------------------------------------------
# The fundamental difference from the fresh briefs: NO ``git reset --hard``. The
# branch and the untracked files in the persistent per-channel workspace are the
# partial work from a previous attempt and MUST be preserved. The brief injects
# the journal (.deile-progress.md), the current diff and tells the agent to read
# every untracked file so it continues with the SAME context, then write an
# updated journal before it stops.

_WORKER_IMPLEMENT_RESUME_BRIEF = """\
RETOMADA da issue #{number} do repositório {repo} — uma tentativa ANTERIOR já começou este trabalho. NÃO recomece do zero, NÃO descarte nada.

Passo a passo:
1. Trabalhe na subpasta ./repo do seu diretório atual (ela JÁ existe com o trabalho parcial). NÃO rode `git reset --hard`, NÃO recrie a branch, NÃO apague arquivos não rastreados — eles são o progresso da tentativa anterior.
2. Faça checkout no branch {branch} se ainda não estiver nele (ele já existe localmente): git checkout {branch}
3. RECONSTRUA O CONTEXTO antes de qualquer edição:
   a) Leia o journal de progresso da tentativa anterior (o que já fiz / o que falta / decisões / bloqueios):
{progress_block}
   b) Veja o diff acumulado em relação a {main}: git diff {main}...HEAD ; e também: git diff HEAD
   c) LEIA TODOS os arquivos não rastreados (untracked) e os modificados — eles contêm o trabalho parcial: git status --porcelain ; depois leia cada arquivo listado.
4. CONTINUE a implementação de onde parou. Crie/edite o que falta e garanta testes cobrindo todos os casos.
5. Rode os testes e garanta 100% de aprovação. As dependências (incl. pytest) JÁ estão no ambiente — NÃO rode `pip install` (filesystem read-only). Para rodar só os testes novos sem o gate global de cobertura: python3 -m pytest <arquivos_de_teste_novos> -p no:cov -q
6. Faça commit normal (SEM force-push) e `git push -u origin {branch}`.
7. ABRA A PR (OBRIGATÓRIO — sem PR a tarefa NÃO está concluída):
   gh pr create --repo {repo} --base {main} --head {branch} --title "<título coerente>" --body "<resumo>. Closes #{number}."
   (Se já existe uma PR para {branch}, apenas confirme-a: gh pr view {branch} --repo {repo} --json url -q .url)
8. ANTES DE PARAR (concluindo OU pausando de novo), ATUALIZE o journal `.deile-progress.md` no diretório de trabalho (NÃO dentro de ./repo, e NÃO commite): registre o que fez, o que falta, decisões-chave e qualquer bloqueio.
9. Se um IMPEDIMENTO REAL impedir continuar (falta credencial/segredo, dependência impossível, decisão de produto pendente), escreva numa linha começando com `BLOQUEADO: <motivo concreto>` — só você sabe disso; o pipeline respeita isso e para de retomar.
10. Na ÚLTIMA LINHA: a URL da PR confirmada (ex.: https://github.com/{repo}/pull/NN), ou, se bloqueado, a linha `BLOQUEADO: <motivo>`. Nada depois dela.

DEFINITION OF DONE: existe uma PR aberta cuja URL você confirmou via gh. NUNCA invente URL nem diga "concluído" sem a PR existir.

=== Issue #{number}: {title} ===
{body}
"""

_WORKER_REVIEW_RESUME_BRIEF = """\
RETOMADA do QUALITY GATE da Pull Request #{number} do repositório {repo} — uma tentativa anterior já começou. NÃO descarte o trabalho parcial. Testes verdes NÃO bastam.

1. Use o clone existente em ./repo (NÃO rode `git reset --hard`, NÃO apague untracked). Garanta o checkout da PR: gh pr checkout {number}
2. RECONSTRUA O CONTEXTO: leia o journal da tentativa anterior e o diff/untracked atuais:
{progress_block}
   git diff {main}...HEAD ; git status --porcelain (leia cada arquivo modificado/untracked listado).
3. AVALIE com RIGOR contra o checklist do revisor (corretude/IDEMPOTÊNCIA — re-dispara a cada tick sem claim/dedup?; SOLID/SRP/DRY/KISS; arquitetura hexagonal; SEGURANÇA — injeção em jq/shell/SQL, segredo em log; error handling tipado; testes de borda + a regressão alegada; packaging — arquivo novo no COPY do Dockerfile e no allowlist do .dockerignore). Anote cada achado (arquivo:linha + problema).
4. CORRIJA os achados com commits normais (SEM force-push) e dê push. Adicione os testes que faltarem.
5. Rode os testes e garanta 100% de aprovação. Dependências JÁ instaladas — NÃO rode `pip install`. Testes do PR sem o gate global: python3 -m pytest <arquivos> -p no:cov -q.
6. DOCUMENTE as evidências como comentário na PR (gh pr comment {number} --repo {repo} --body "...").
7. VEREDITO — só mergeie se o checklist passou E os testes estão verdes: gh api -X PUT repos/{repo}/pulls/{number}/merge -f merge_method=merge (fallback: gh pr merge {number} --repo {repo} --merge). Confirme: gh pr view {number} --repo {repo} --json merged -q .merged (deve ser true).
8. ANTES DE PARAR, atualize `.deile-progress.md` no diretório de trabalho (fora de ./repo, sem commitar): o que revisou, achados, correções e o que falta.
9. Se um impedimento real impedir o merge com qualidade, escreva `BLOQUEADO: <motivo concreto>`. Caso contrário, na ÚLTIMA LINHA escreva a URL da PR seguida de MERGED. NUNCA escreva MERGED sem ter mergeado de fato; NUNCA invente resultado.
"""

_WORKER_MENTION_BRIEF = """\
Você foi acionado por {trigger_summary} no repositório {repo}.

Contexto completo dos gatilhos detectados:
{trigger_details}

Ação esperada:
{expected_action}

IMPORTANTE:
- Se for um ASSIGNEE em uma issue SEM PR aberta, implemente a issue completa (use o fluxo normal de implementação: branch + commit + teste + PR).
- Se for um ASSIGNEE em uma issue que JÁ TEM PR aberta, verifique se a PR cobre tudo da issue. Se não cobrir, faça checkout do branch da PR e continue a implementação.
- Se for REQUESTED REVIEWER, faça review completa (arquitetura, DRY, KISS, SOLID, clean code) + teste + corrija o que precisar + commit + push + comente as evidências + merge.
- Se for MENTION em comentário, responda diretamente ao que foi pedido. Se o comentário está numa PR, trabalhe no branch da PR.
- Poste SEMPRE a resposta ou evidências como comentário no GitHub.
- Na ÚLTIMA LINHA, escreva a URL relevante (PR, issue, etc).
"""

# Context-aware worker mention brief builder (issue #253)
def _render_worker_mention_brief(
    repo: str,
    ref: "MentionTrigger",
    trigger_types: list[str],
    all_triggers: list["MentionTrigger"],
) -> str:
    """Build a context-rich mention brief from all trigger types."""
    # Summarize trigger types
    type_labels = {
        "assignee": "assignee (atribuído a você)",
        "reviewer": "reviewer (solicitado como revisor)",
        "comment": "menção em comentário (@deile-one)",
        "body": "menção no corpo (@deile-one)",
    }
    trigger_summary = " + ".join(type_labels.get(t, t) for t in trigger_types)

    # Detailed trigger info
    details_parts: list[str] = []
    for t in all_triggers:
        if t.trigger_type == "comment" and t.comment is not None:
            details_parts.append(
                f"- **Comentário** de @{t.comment.author} em {t.comment.html_url}:\n"
                f"  ```\n  {t.comment.body[:500]}\n  ```"
            )
        elif t.trigger_type == "assignee" and t.issue is not None:
            details_parts.append(
                f"- **Assignado** na issue #{t.issue.number}: [{t.issue.title}]({t.issue.url})"
            )
        elif t.trigger_type == "assignee" and t.pr is not None:
            details_parts.append(
                f"- **Assignado** na PR #{t.pr.number}: [{t.pr.title}]({t.pr.url})"
            )
        elif t.trigger_type == "reviewer" and t.pr is not None:
            details_parts.append(
                f"- **Solicitado como reviewer** na PR #{t.pr.number}: [{t.pr.title}]({t.pr.url})"
            )
        elif t.trigger_type == "body":
            if t.issue is not None:
                details_parts.append(
                    f"- **Menção no corpo** da issue #{t.issue.number}: [{t.issue.title}]({t.issue.url})"
                )
            elif t.pr is not None:
                details_parts.append(
                    f"- **Menção no corpo** da PR #{t.pr.number}: [{t.pr.title}]({t.pr.url})"
                )
    trigger_details = "\n".join(details_parts)

    # Determine expected action
    target_kind = ref.target_kind
    has_assignee = "assignee" in trigger_types
    has_reviewer = "reviewer" in trigger_types
    has_comment = "comment" in trigger_types
    has_body = "body" in trigger_types

    if has_reviewer and target_kind == "pr":
        expected_action = (
            f"**REVIEW REQUEST**: Você foi solicitado como revisor da PR #{ref.target_number}. "
            f"Faça uma revisão completa (arquitetura, DRY, KISS, SOLID, clean code), "
            f"rode os testes, corrija problemas, faça commit + push, poste evidências como "
            f"comentário na PR e faça o merge."
        )
    elif has_assignee and target_kind == "issue":
        expected_action = (
            f"**ASSIGNED**: Você foi atribuído à issue #{ref.target_number}. "
            f"Implemente a feature completa, crie testes, abra uma PR. "
            f"Se já existir uma PR para esta issue, verifique se cobre tudo e "
            f"continue a implementação no branch existente."
        )
    elif has_assignee and target_kind == "pr":
        expected_action = (
            f"**ASSIGNED TO PR**: Você foi atribuído à PR #{ref.target_number}. "
            f"Revise, corrija, teste, faça commit + push e mergeie se estiver pronto."
        )
    elif has_comment or has_body:
        if target_kind == "pr":
            expected_action = (
                f"**MENTION ON PR**: Você foi mencionado na PR #{ref.target_number}. "
                f"Atenda ao que foi pedido no comentário/corpo, trabalhe no branch da PR, "
                f"teste, faça commit + push e poste a resposta como comentário na PR."
            )
        else:
            expected_action = (
                f"**MENTION ON ISSUE**: Você foi mencionado na issue #{ref.target_number}. "
                f"Atenda ao que foi pedido no comentário/corpo. "
                f"Se a issue já tem PR aberta, trabalhe no branch da PR."
            )
    else:
        expected_action = "Atenda ao contexto acima da forma mais apropriada."

    return _WORKER_MENTION_BRIEF.format(
        repo=repo,
        trigger_summary=trigger_summary,
        trigger_details=trigger_details,
        expected_action=expected_action,
    )


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


# The journal lives in the worker's per-channel PVC workspace (one level above
# ./repo), written by a previous attempt — the PIPELINE cannot inject its
# content (it has no access to the worker filesystem), so the brief instructs
# the worker to read its OWN local copy. This is the ``{progress_block}`` text.
_PROGRESS_BLOCK = (
    "      Leia o arquivo `.deile-progress.md` no SEU diretório de trabalho "
    "(um nível acima de ./repo). Ele foi escrito pela tentativa anterior — é o "
    "ponto de partida. Se não existir, reconstrua o contexto pelo diff e pelos "
    "arquivos untracked nos passos abaixo."
)


def _render_worker_implement_resume_brief(
    repo: str, main: str, branch: str, number: int, title: str, body: str
) -> str:
    return _WORKER_IMPLEMENT_RESUME_BRIEF.format(
        repo=repo,
        main=main,
        branch=branch,
        number=number,
        title=title,
        body=(body or "").strip()[:ISSUE_BODY_MAX_CHARS] or "(sem corpo — continue a partir do título e do trabalho parcial)",
        progress_block=_PROGRESS_BLOCK,
    )


def _render_worker_review_resume_brief(
    repo: str, main: str, number: int, title: str
) -> str:
    return _WORKER_REVIEW_RESUME_BRIEF.format(
        repo=repo, main=main, number=number, title=title, progress_block=_PROGRESS_BLOCK
    )


# --- Reviewer-only brief (issue #253 follow-up) -------------------------------
# When DEILE is requested ONLY as a reviewer (not assignee/owner), the operator
# policy is: REVIEW and hand the PR back to its author — never fix, never merge.
# DEILE submits a review via REST and sets the PR author as assignee (even when
# the author is DEILE itself). GitHub removes a requested reviewer from the
# "requested" set once they submit a review, so this is naturally idempotent.
_WORKER_REVIEW_ONLY_BRIEF = """\
Você foi solicitado APENAS como REVISOR da Pull Request #{number} do repositório {repo}. Seu papel é SÓ revisar e DEVOLVER ao autor — NÃO corrija o código, NÃO faça commits, NÃO mergeie. Execute de verdade.

1. Garanta um clone atualizado de {repo} em ./repo (gh repo clone {repo} repo se não existir; senão git fetch origin). Dentro de ./repo: gh pr checkout {number}.
2. LEIA O DIFF INTEIRO e entenda a intenção: git diff {main}...HEAD. Leia cada arquivo tocado.
3. AVALIE com RIGOR contra o checklist do revisor (corretude/IDEMPOTÊNCIA — re-dispara a cada tick sem claim/dedup?; SOLID/SRP/DRY/KISS; arquitetura hexagonal; SEGURANÇA — injeção em jq/shell/SQL, segredo em log; error handling tipado; testes de borda; packaging — arquivo novo no COPY do Dockerfile e no allowlist). Anote cada achado com arquivo:linha.
4. POSTE a review via REST (você pode NÃO ser o autor; mesmo assim use REST para evitar escopos extras): gh api -X POST repos/{repo}/pulls/{number}/reviews -f event=COMMENT -f body="<resumo dos achados, arquivo:linha, e o que precisa mudar>". Use event=REQUEST_CHANGES se houver bloqueio; COMMENT se forem sugestões. NÃO use APPROVE (a decisão de merge é do autor/assignee).
5. DEVOLVA ao autor: descubra o autor (AUTOR=$(gh pr view {number} --repo {repo} --json author -q .author.login)) e marque-o como ASSIGNEE: gh api -X POST repos/{repo}/issues/{number}/assignees -f "assignees[]=$AUTOR". (Mesmo que o autor seja você — é o sinal de "bola de volta pro autor".)
6. NÃO mergeie, NÃO faça commits de correção. Seu trabalho termina ao postar a review e devolver ao autor.
7. Na ÚLTIMA LINHA escreva a URL da PR (https://github.com/{repo}/pull/{number}). Se algo REAL impediu a review, escreva `BLOQUEADO: <motivo concreto>`. NUNCA invente um resultado.
"""


# --- Address-PR brief: comment/body mention on a PR (no merge) ----------------
# A @mention in a PR comment or body asks DEILE to DO what was requested on that
# PR. It may fix code, but it must NOT merge (only the assignee finalizes a PR).
# It also resolves any pending review threads with critical judgement.
_WORKER_PR_ADDRESS_BRIEF = """\
Você foi MENCIONADO na Pull Request #{number} do repositório {repo} (em comentário ou no corpo). Atenda ao que foi pedido — execute de verdade. NÃO mergeie (o merge é do autor/assignee).

1. Garanta um clone atualizado de {repo} em ./repo; dentro dela: gh pr checkout {number}.
2. Leia o contexto do que foi pedido (o comentário/corpo que te mencionou) e o diff atual (git diff {main}...HEAD).
3. THREADS/NOTAS pendentes (gh api repos/{repo}/pulls/{number}/comments): para CADA uma, JULGUE criticamente se o que foi pedido está realmente correto. Se procede, FAÇA a mudança e responda a thread citando o commit. Se NÃO procede, responda com JUSTIFICATIVA concreta. Não deixe thread sem ação ou justificativa.
4. Se a tarefa envolve código, edite, rode os testes (NÃO rode pip install — deps já instaladas; python3 -m pytest <arquivos> -p no:cov -q), faça commit normal (SEM force-push) e push.
5. COMENTE o resultado na PR (gh pr comment {number} --repo {repo} --body "..."): o que fez, como tratou cada thread, e a saída real dos testes.
6. NÃO mergeie. Na ÚLTIMA LINHA escreva a URL da PR. Se um impedimento real surgir, escreva `BLOQUEADO: <motivo concreto>`. NUNCA invente resultado.
"""


def _render_worker_review_only_brief(repo: str, main: str, number: int) -> str:
    return _WORKER_REVIEW_ONLY_BRIEF.format(repo=repo, main=main, number=number)


def _render_worker_pr_address_brief(repo: str, main: str, number: int) -> str:
    return _WORKER_PR_ADDRESS_BRIEF.format(repo=repo, main=main, number=number)


# (removed duplicate _render_worker_mention_brief — the context-aware version above is authoritative)


def _build_resume_block(
    repo: str,
    main: str,
    branch: str,
    *,
    resume: bool,
    expect_merge: bool,
    pr_url_hint: str = "",
) -> dict:
    """Assemble the ``resume`` wire block sent to the worker (issue #254).

    Sent on EVERY pipeline dispatch (fresh and resume) so the worker always
    returns a structured ground-truth result and seeds ``.deile-progress.json``
    — ``mode`` tells the worker whether this was a fresh start or a resume, but
    the brief (not this block) decides reset-vs-keep. ``expect_merge`` is True
    for the review/merge stage so "done" requires a confirmed merge, not just a
    PR URL.
    """
    return {
        "mode": "resume" if resume else "fresh",
        "repo": repo,
        "branch": branch,
        "main_branch": main,
        "expect_merge": expect_merge,
        "pr_url_hint": pr_url_hint,
    }


def _outcome_from_worker_response(data: object) -> WorkOutcome:
    """Map a worker dispatch response dict to a :class:`WorkOutcome`.

    Reads the legacy ``ok``/``summary``/``error`` fields AND the structured
    ``resume`` block (issue #254) when present, so the stage handler gets the
    ground-truth ``ended``/``pr_url``/``motivo_bloqueio``/``fingerprint``/
    ``tentativa`` without re-parsing the worker's free-text summary.
    """
    if not isinstance(data, dict):
        return WorkOutcome(ok=False, text="", error="worker returned non-dict response")
    ok = bool(data.get("ok"))
    text = str(data.get("summary") or "")
    resume_block = data.get("resume")
    fields: dict = {}
    if isinstance(resume_block, dict):
        fields = {
            "ended": str(resume_block.get("ended") or ""),
            "pr_url": str(resume_block.get("pr_url") or ""),
            "motivo_bloqueio": str(resume_block.get("motivo_bloqueio") or ""),
            "motivo_fim_loop": str(resume_block.get("motivo_fim_loop") or ""),
            "fingerprint": str(resume_block.get("fingerprint") or ""),
            "tentativa": int(resume_block.get("tentativa") or 0),
            "budget_acumulado_s": float(resume_block.get("budget_acumulado_s") or 0.0),
        }
    if ok:
        return WorkOutcome(ok=True, text=text, error="", **fields)
    err = str(data.get("error") or data.get("summary") or "worker reported failure")
    return WorkOutcome(ok=False, text=text, error=err[:500], **fields)


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

    async def _dispatch(
        self,
        brief: str,
        *,
        channel_id: str,
        persona: str = "developer",
        resume_block: Optional[dict] = None,
    ) -> WorkOutcome:
        from deile.infrastructure.deile_worker_client import (
            WorkerDispatchError, build_dispatch_payload)

        payload = build_dispatch_payload(
            brief=brief, channel_id=channel_id, persona=persona, wait=True
        )
        # The resume context (issue #254) is an additive wire field consumed by
        # the worker; ``build_dispatch_payload`` validates the core fields, so
        # we attach ``resume`` after building to keep that contract untouched.
        if resume_block:
            payload["resume"] = resume_block
        try:
            data = await self._client.dispatch(payload, wait=True)
        except WorkerDispatchError as exc:
            return WorkOutcome(ok=False, text="", error=f"{exc.error_code}: {exc}"[:500])
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.exception("worker dispatch raised")
            return WorkOutcome(ok=False, text="", error=f"{type(exc).__name__}: {exc}"[:500])
        return _outcome_from_worker_response(data)

    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        branch = monitor.branch_for_issue(issue.number)
        if resume:
            brief = _render_worker_implement_resume_brief(
                monitor.config.repo, monitor.config.main_branch, branch,
                issue.number, issue.title, issue.body,
            )
        else:
            brief = _render_worker_implement_brief(
                monitor.config.repo, monitor.config.main_branch, branch,
                issue.number, issue.title, issue.body,
            )
        resume_block = _build_resume_block(
            monitor.config.repo, monitor.config.main_branch, branch,
            resume=resume, expect_merge=False,
        )
        return await self._dispatch(
            brief, channel_id=f"pipeline-issue-{issue.number}", resume_block=resume_block
        )

    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        if resume:
            brief = _render_worker_review_resume_brief(
                monitor.config.repo, monitor.config.main_branch, pr.number, pr.title
            )
        else:
            brief = _render_worker_review_brief(
                monitor.config.repo, monitor.config.main_branch, pr.number, pr.title
            )
        resume_block = _build_resume_block(
            monitor.config.repo, monitor.config.main_branch,
            pr.head_ref or f"pr/{pr.number}", resume=resume, expect_merge=True,
            pr_url_hint=pr.url,
        )
        # The review/merge stage is the final quality gate: dispatch under the
        # ``reviewer`` persona (instructions in personas/instructions/reviewer.md)
        # so the worker evaluates SOLID/SRP/DRY/KISS/security/idempotency, not
        # just whether the suite is green. implement/mention keep ``developer``.
        return await self._dispatch(
            brief, channel_id=f"pipeline-pr-{pr.number}",
            persona="reviewer", resume_block=resume_block,
        )

    async def mention(
        self,
        monitor: "PipelineMonitor",
        ref: "MentionTrigger",
        *,
        trigger_types: list[str] | None = None,
        all_triggers: list["MentionTrigger"] | None = None,
        mode: str = "comment",
        resume: bool = False,
    ) -> WorkOutcome:
        """Dispatch a mention/assignment by ROLE (issue #253 follow-up).

        ``mode`` (decided by the stage router) selects the brief + persona:

        - ``review_only`` — requested reviewer: review + assign author back, NO
          fix/merge (reviewer persona).
        - ``work_merge`` — assignee on a PR: quality-gate review + resolve
          threads + fix + MERGE (reviewer persona, resume-aware).
        - ``address`` — comment/body mention on a PR: do what was asked +
          resolve threads + push, NO merge (reviewer persona).
        - ``comment`` — comment mention on an issue: do what the comment says
          (developer persona, context-rich brief). Default.
        """
        repo = monitor.config.repo
        main = monitor.config.main_branch
        number = ref.target_number
        channel_id = f"pipeline-mention-{ref.target_kind}-{number}"
        pr_ref = next(
            (t.pr for t in (all_triggers or [ref]) if t.pr is not None), None
        )
        head = (pr_ref.head_ref if pr_ref else "") or f"pr/{number}"
        pr_url_hint = pr_ref.url if pr_ref else ""

        if mode == "review_only":
            brief = _render_worker_review_only_brief(repo, main, number)
            return await self._dispatch(
                brief, channel_id=channel_id, persona="reviewer",
                resume_block=_build_resume_block(
                    repo, main, head, resume=resume, expect_merge=False,
                    pr_url_hint=pr_url_hint,
                ),
            )
        if mode == "work_merge":
            brief = (
                _render_worker_review_resume_brief(repo, main, number, "")
                if resume else _render_worker_review_brief(repo, main, number, "")
            )
            return await self._dispatch(
                brief, channel_id=channel_id, persona="reviewer",
                resume_block=_build_resume_block(
                    repo, main, head, resume=resume, expect_merge=True,
                    pr_url_hint=pr_url_hint,
                ),
            )
        if mode == "address":
            brief = _render_worker_pr_address_brief(repo, main, number)
            return await self._dispatch(
                brief, channel_id=channel_id, persona="reviewer",
                resume_block=_build_resume_block(
                    repo, main, head, resume=resume, expect_merge=False,
                    pr_url_hint=pr_url_hint,
                ),
            )
        # Default: comment mention on an issue → do what the comment says.
        brief = _render_worker_mention_brief(
            repo, ref, trigger_types or [], all_triggers or [],
        )
        return await self._dispatch(brief, channel_id=channel_id, persona="developer")


# ---------------------------------------------------------------------------
# Claude mention prompt builder (issue #253)
# ---------------------------------------------------------------------------

def _render_claude_mention_prompt(
    repo: str,
    ref: "MentionTrigger",
    trigger_types: list[str],
    all_triggers: list["MentionTrigger"],
) -> str:
    """Build a context-rich mention prompt for the Claude path."""
    type_labels = {
        "assignee": "assignee (atribuído a você)",
        "reviewer": "reviewer (solicitado como revisor)",
        "comment": "menção em comentário (@deile-one)",
        "body": "menção no corpo (@deile-one)",
    }
    trigger_summary = " + ".join(type_labels.get(t, t) for t in trigger_types)

    details_parts: list[str] = []
    for t in all_triggers:
        if t.trigger_type == "comment" and t.comment is not None:
            details_parts.append(
                f"- Comentário de @{t.comment.author} em {t.comment.html_url}:\n"
                f"  {t.comment.body[:500]}"
            )
        elif t.trigger_type == "assignee" and t.issue is not None:
            details_parts.append(
                f"- Assignado na issue #{t.issue.number}: {t.issue.title} ({t.issue.url})"
            )
        elif t.trigger_type == "assignee" and t.pr is not None:
            details_parts.append(
                f"- Assignado na PR #{t.pr.number}: {t.pr.title} ({t.pr.url})"
            )
        elif t.trigger_type == "reviewer" and t.pr is not None:
            details_parts.append(
                f"- Solicitado como reviewer na PR #{t.pr.number}: {t.pr.title} ({t.pr.url})"
            )
        elif t.trigger_type == "body":
            if t.issue is not None:
                details_parts.append(
                    f"- Menção no corpo da issue #{t.issue.number}: {t.issue.title} ({t.issue.url})"
                )
            elif t.pr is not None:
                details_parts.append(
                    f"- Menção no corpo da PR #{t.pr.number}: {t.pr.title} ({t.pr.url})"
                )
    trigger_details = "\n".join(details_parts)

    target_kind = ref.target_kind
    has_assignee = "assignee" in trigger_types
    has_reviewer = "reviewer" in trigger_types
    has_comment = "comment" in trigger_types
    has_body = "body" in trigger_types

    if has_reviewer and target_kind == "pr":
        action = (
            f"REVIEW REQUEST: Revise a PR #{ref.target_number} completamente "
            f"(arquitetura, DRY, KISS, SOLID, clean code), rode testes, corrija, "
            f"commit + push, comente evidências e faça merge."
        )
    elif has_assignee and target_kind == "issue":
        action = (
            f"ASSIGNED: Implemente a issue #{ref.target_number} completa. "
            f"Crie testes, abra PR. Se já existir PR, continue no branch existente."
        )
    elif has_assignee and target_kind == "pr":
        action = (
            f"ASSIGNED TO PR: Revise/corrija a PR #{ref.target_number}, "
            f"teste, commit + push e mergeie."
        )
    elif has_comment or has_body:
        if target_kind == "pr":
            action = (
                f"MENTION ON PR: Atenda ao pedido na PR #{ref.target_number}, "
                f"trabalhe no branch da PR, teste, commit + push, comente resposta."
            )
        else:
            action = (
                f"MENTION ON ISSUE: Atenda ao pedido na issue #{ref.target_number}. "
                f"Se a issue já tem PR aberta, trabalhe no branch da PR."
            )
    else:
        action = "Atenda ao contexto acima da forma mais apropriada."

    return (
        f"Você foi acionado por {trigger_summary} no repositório {repo}.\n\n"
        f"Contexto:\n{trigger_details}\n\n"
        f"Ação esperada:\n{action}\n\n"
        f"IMPORTANTE: Poste a resposta como comentário no GitHub usando gh. "
        f"Na última linha, escreva a URL relevante."
    )


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
