"""Worker/Claude brief templates and renderers for the autonomous pipeline.

⚠ CRITICAL: QUALQUER mudança nestes briefs exige (a) teste de regressão
asserting substrings críticas (`CHECKPOINT OBRIGATÓRIO`, `SUÍTE COMPLETA`,
`anti-eco`, `.deile-progress.md`, `DECISÃO É DECISÃO`, `REGRA ANTI-FLOOD`,
`execute o pedido SEMPRE`), (b) comparação A/B do custo médio por sessão
antes/depois (PR #469 reduziu de $27 → ~$5-8/sessão), (c) validação
empírica em PR real antes de mergear.

These are the imperative PT-BR prompts the pipeline sends to the executing
agent — the long-running ``deile-worker`` Pod (markdown briefs) or the legacy
``claude -p`` path (plain-prose prompt).

Forge-agnostic (issue #297): every CLI command in the templates is now a
``{forge_*_cmd}`` placeholder filled by
:func:`deile.orchestration.forge.cli_renderer.render_brief_cmds`. The same
template renders to ``gh ...`` for a GitHub project and to ``glab ...`` for
a GitLab project — the worker only ever sees the right command for the
right forge. When a caller does not pass an explicit :class:`ForgeConfig`
(test code that bypassed the migration) the renderer falls back to a
default GitHub configuration on ``github.com`` so the rendered text matches
the legacy byte-exact GH output.
"""
# Brief unificado pr_unified — refactor #45 / PR #411

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any, Optional

from deile.orchestration.forge.base import ForgeConfig, ForgeKind
from deile.orchestration.forge.cli_renderer import render_brief_cmds
from deile.orchestration.pipeline.constants import ISSUE_BODY_MAX_CHARS
from deile.orchestration.pipeline.labels import title_prefix_for_type

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.forge.refs import MentionTrigger


# ---------------------------------------------------------------------------
# Shared brief building blocks (PR #275 quality-gate uniformity)
#
# These constants are interpolated into the worker briefs at render time. They
# centralize phrases that appear verbatim in 5+ briefs (the full-suite test
# command, the pip-install guard, the BLOQUEADO contract) so a policy change
# (e.g. moving the gate command, adding a new artifact to upload) is a 1-line
# edit, not a sweep across 7 templates.
# ---------------------------------------------------------------------------
_FULL_SUITE_CMD = "python3 -m pytest deile/tests/ -q"
_PIP_GUARD = "deps já instaladas — NÃO rode `pip install` (filesystem read-only)"
_BLOCKED_CONTRACT = "BLOQUEADO: <motivo concreto>"

# Estratégia de testes para o IMPLEMENTADOR: análise de impacto, não suíte completa.
# O revisor (quality gate) é quem roda a suíte inteira — o implementador só precisa
# garantir que os testes relevantes à sua mudança estejam verdes.
#
# Nota: os valores de _PIP_GUARD e _FULL_SUITE_CMD são embutidos diretamente para
# evitar conflito de placeholder com o str.format() do _render_brief (single-pass).
_IMPACT_TEST_STRATEGY = (
    "TESTES — análise de impacto (NÃO rode a suíte completa; revisor faz isso):\n"
    "   a) `git diff --name-only HEAD` → lista do que editou.\n"
    "   b) Para cada arquivo `deile/X/Y/z.py`: rode `deile/tests/X/Y/test_*.py` + "
    "qualquer `grep -rln \"from deile.X.Y\" deile/tests/`. Mesma regra por subpacote.\n"
    "   c) Comando: `python3 -m pytest <lista_de_testes_impactados> -p no:cov -q`. "
    + _PIP_GUARD + ". Itere até verde.\n"
    "   NUNCA rode `" + _FULL_SUITE_CMD + "` (suíte inteira) — é tarefa do revisor."
)

_BRIEF_CONSTANTS: dict[str, Any] = {
    "full_suite_cmd": _FULL_SUITE_CMD,
    "pip_guard": _PIP_GUARD,
    "blocked_contract": _BLOCKED_CONTRACT,
    "impact_test_strategy": _IMPACT_TEST_STRATEGY,
}


def _default_forge_config(repo: str) -> ForgeConfig:
    """Build a default :class:`ForgeConfig` for GitHub cloud.

    Used by the brief renderers when the caller did not supply one — keeps
    backwards compatibility with every existing test that calls
    ``_render_worker_implement_brief("owner/repo", "main", ...)`` without
    knowing about forges.
    """
    # ``shutil.which`` may return None in test sandboxes that lack ``gh``;
    # the cli_path is only used inside the brief text (not actually
    # executed at render time) so falling back to the bare command name is
    # safe and keeps tests hermetic.
    cli = shutil.which("gh") or "gh"
    return ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path=repo,
        cli_path=cli,
    )


# Definition-of-Done evidence gate, injected as ``{dod_block}`` into the
# implement / implement-resume briefs. Closes the structural hole that let a
# worker open an issue-closing PR whose hard ACs were never executed (skipped
# integration tests count as "green"): "a PR exists + impacted tests pass" is
# NOT done when the issue declares empirical acceptance criteria. Pre-formatted
# in :func:`_build_brief_params` (embeds the per-forge draft/comment commands).
_DOD_EVIDENCE_BLOCK = (
    "DEFINIÇÃO DE PRONTO — confronte ENTREGA vs os Critérios de Aceite (ACs) da issue. "
    "\"Compila + PR existe\" NÃO é pronto:\n"
    "   a) AC com número/condição testável (HTTP 200, custo %, p95, trigger %) só está PRONTO "
    "com o AC EXECUTADO e o NÚMERO real anexado ao corpo da PR/relatório. Scaffolding/harness "
    "escrito ≠ AC cumprido.\n"
    "   b) Teste `@pytest.mark.integration` que PULA (skip por falta de credencial/env) NÃO "
    "satisfaz o AC — rode-o com a credencial disponível no pod (ex.: `ANTHROPIC_AUTH_TOKEN` / "
    "`~/.claude/credentials.json`) ou marque o AC como NÃO-VERIFICADO no corpo da PR.\n"
    "   c) AC duro que você NÃO conseguiu provar (bloqueado): NÃO feche a issue. Abra a PR como "
    "DRAFT (`{mark_draft_cmd}` após criar), use `Refs #{number}` (não `Closes`) no --body, "
    "registre a causa do bloqueio e escale ao autor (`{comment_issue_cmd}`). Nunca uma PR "
    "`Closes` com ACs vazios.\n"
    "   d) SPIKE (entregável = evidência, não código de produção): o `create_pr_cmd` abaixo já "
    "usa `Refs` (não fecha a issue). Abra DRAFT e só tire do draft — trocando para `Closes` — "
    "quando TODOS os ACs estiverem verdes com números anexados."
)


def _is_spike(title: str, body: str) -> bool:
    """True when the issue's deliverable is empirical evidence, not production code.

    A spike's Definition-of-Done is measured ACs (numbers/verdict), so its PR must
    reference (``Refs``) — never auto-close (``Closes``) — the issue, and should
    stay draft until every AC is green. Detected by the conventional ``[SPIKE]``
    title tag or a spike-style exit-condition section in the body. Conservative on
    purpose: a missed spike (defaults to ``Closes``) is the dangerous direction, so
    we only require an explicit, unambiguous signal.
    """
    t = (title or "").lower()
    b = (body or "").lower()
    if "[spike]" in t:
        return True
    return any(
        marker in b
        for marker in ("condição de saída", "critérios de aprovação do spike")
    )


def _build_brief_params(
    *,
    repo: str,
    main: str,
    branch: str,
    number: int,
    forge: Optional[ForgeConfig],
    issue_template: str = "feature_request.md",
    close_keyword: str = "Closes",
    extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble the full ``{key: value}`` map for ``.format()`` on a template.

    Combines: the shared constants (test command, pip guard, BLOQUEADO),
    the runtime values (``repo``/``main``/``branch``/``number``) and the
    per-forge CLI snippets from :func:`render_brief_cmds`. Extras override
    everything else so callers can inject brief-specific fields
    (``title``/``body``/``progress_block`` …) without surprises.

    ``close_keyword`` flows down to :func:`render_brief_cmds` so spike briefs
    render ``create_pr_cmd`` with ``Refs`` instead of ``Closes`` (a spike PR
    must reference, never auto-close, its issue). The ``dod_block`` placeholder
    (Definition-of-Done evidence gate) is pre-formatted here because it embeds
    the per-forge ``mark_draft_cmd``/``comment_issue_cmd`` and the issue number.
    """
    cfg = forge or _default_forge_config(repo)
    cmds = render_brief_cmds(
        cfg, number=number, branch=branch, main=main,
        issue_template=issue_template, close_keyword=close_keyword,
    )
    params: dict[str, Any] = dict(_BRIEF_CONSTANTS)
    params.update({
        "repo": repo,
        "main": main,
        "branch": branch,
        "number": number,
    })
    params.update(cmds)
    params["dod_block"] = _DOD_EVIDENCE_BLOCK.format(
        number=number,
        mark_draft_cmd=cmds["mark_draft_cmd"],
        comment_issue_cmd=cmds["comment_issue_cmd"],
    )
    if extras:
        params.update(extras)
    return params


def _render_brief(template: str, *, params: dict[str, Any]) -> str:
    """Format a brief template with a pre-built params dict."""
    return template.format(**params)


# ---------------------------------------------------------------------------
# Implement / review briefs
# ---------------------------------------------------------------------------


_WORKER_IMPLEMENT_BRIEF = """\
Implemente a issue #{number} de {repo} e abra uma {pr_noun}. Execute de verdade — não invente URL nem diga "concluído" sem a {pr_noun} existir.

1. ./repo: se não existir → `{clone_cmd}`; se existir → `git fetch origin && git checkout {main} && git reset --hard origin/{main}`.
2. Crie branch `{branch}` a partir de `{main}`.
3. Leia comentários da issue (`{view_issue_cmd}`) — decisões do stakeholder FAZEM PARTE do escopo. Implemente o pedido + testes. CHECKPOINT INCREMENTAL: grave `.deile-progress.md` (um nível acima de ./repo, NÃO commite, NÃO em ./repo) a cada milestone — o timeout mata o processo sem aviso (rc=124) e só o journal sobrevive pro próximo tick.
4. {impact_test_strategy}
5. {dod_block}
6. Commit atômico + `git push -u origin {branch}`.
7. Abra a {pr_noun} (OBRIGATÓRIO): `{create_pr_cmd}`. No --body, confronte ENTREGA vs cada AC (com números) — não um resumo genérico.
8. Confirme: `{check_pr_cmd}` — se não retornar URL, volte ao passo 7.
9. NÃO force-push. NÃO altere nada fora de ./repo. ÚLTIMA LINHA = URL da {pr_noun} (ex: {pr_url_pattern}).

=== Issue #{number}: {title} ===
{body}
"""

# --- Unified PR brief (refactor "PR é o quadro") ------------------------------
# Substitui ``_WORKER_REVIEW_BRIEF``, ``_WORKER_REVIEW_ONLY_BRIEF`` e
# ``_WORKER_PR_ADDRESS_BRIEF``. O princípio inegociável: a PR é o quadro; o
# trigger só diz QUAL PR olhar. O worker abre a PR, descobre o estado real e
# monta a work-list a partir DELE — não do trigger que o acordou. Resume é
# coberto pelo PASSO 0 lendo ``.deile-progress.md``; merge só acontece quando
# o autor sou eu (PR própria auto/issue-N): o GitHub PROÍBE aprovar a própria PR
# (422), então NÃO existe review formal APPROVED nesse caso — o merge é DIRETO
# com suíte verde + threads ok (autor pode mergear a própria PR sem aprovação).
_WORKER_PR_BRIEF = """\
{pr_noun} #{number} de {repo}. Descubra o que fazer pelo ESTADO REAL da {pr_noun}, não pelo trigger.

CHECKPOINT OBRIGATÓRIO: execução é FALHA sem 1+ comentário visível via {comment_pr_cmd}.

PASSO 0 — Checkout + estado:
1. Clone se preciso ({clone_cmd}); `{checkout_pr_cmd}`.
2. Leia `.deile-progress.md` no diretório de trabalho (um nível acima de ./repo) — TODO da tentativa anterior. Continue de onde parou.
3. Estado da {pr_noun} via REST (use o forge correto — gh OU glab):
   gh api repos/{repo}/pulls/{number} --jq '{{author:.user.login, assignees:[.assignees[].login], requested_reviewers:[.requested_reviewers[].login], head_sha:.head.sha, mergeable:.mergeable}}'
   gh api repos/{repo}/pulls/{number}/reviews --jq '[.[] | select(.user.login == "{gh_login}")] | sort_by(.submitted_at) | last'
4. Calcule: meu_papel (autor/assignee/reviewer com "{gh_login}"); meu_review_atual (commit_id == head_sha); threads_abertas; comment dirigido a mim sem resposta.

PASSO 1 — WORK-LIST (do estado, não do trigger):
- HEAD novo + sou reviewer/assignee → revisar
- thread aberta dirigida a mim → responder/resolver
- comment dirigido a mim sem resposta → atender o pedido
- sou assignee + autor é OUTRO + meu review APPROVED na HEAD + threads ok + CI verde → MERGEAR
- **autor==`{gh_login}` E sou assignee (PR PRÓPRIA, ex: `auto/issue-N`) + SUÍTE VERDE + threads ok + zero regressão funcional → MERGEAR DIRETO** com `{merge_cmd}`. NÃO tente `--approve`/review formal na própria PR: o GitHub rejeita com 422 "Can not approve your own pull request" — por isso APPROVED nunca existe em PR própria, e insistir nisso é EXATAMENTE o que gera o loop de comentário infinito. O autor mergeia a própria PR sem aprovação. Se sobra só AC de doc/qualidade (não bug funcional), MERGEIE assim mesmo e abra UMA sub-issue de follow-up agregada (anti-flood) com o checklist.
- reviewer só, HEAD igual → comentar curto "já APPROVED em <sha>, sem novidade"
- **autor==`{gh_login}` (sou eu) + (meu_review_atual.body OU meu último comentário) pede mudança ("REQUEST_CHANGES"/"CHANGES") → RESOLVER AGORA, nunca re-revisar**: se a SUÍTE está VERDE e não há regressão real, o pedido já está atendido → **MERGEAR** (não re-comente). Se há mudança real pendente, trate como `implement-resume`: leia o achado, escreva o code que atende OU comente UMA vez justificando por que é fora-de-escopo. NUNCA re-revisar/re-comentar a mesma HEAD repetindo o mesmo pedido — esse é o loop infinito. O ciclo é: pediu-mudança → próximo tick IMPLEMENTA ou MERGEIA, nunca re-pede.

REGRA — execute o pedido SEMPRE (não importa o autor):
- {pr_noun} está OPEN → push direto na própria branch.
- {pr_noun} está MERGED/CLOSED → branch nova derivada (`auto/<orig>-followup-<sha>`) a partir de `{main}`, NOVA {pr_noun} mencionando a anterior (`Closes #{number} follow-up`).

PASSO 2 — Executar a work-list:
- NÃO se auto-mencione (anti-eco — identidade vem do `.user.login`).
- Review: `{review_post_cmd}` (APPROVE/REQUEST_CHANGES). Merge: `{merge_cmd}` (fallback `{merge_fallback_cmd}`); confirme `{check_merged_cmd}`. Comment: `{comment_pr_cmd}`.
- Para revisar/mergear: rode a SUÍTE COMPLETA `{full_suite_cmd}` ({pip_guard}). Vermelha por sua mudança = bloqueante; vermelha por testes pré-existentes intocados = documenta e segue.
- **Suite com failures suspeitas de regressão?** ANTES de votar REQUEST_CHANGES, prove via bisseção empírica: `git checkout $(git merge-base origin/{main} HEAD) -- <arquivos modificados pela PR> && pytest <testes falhando> -q && git checkout HEAD -- <mesmos arquivos>`. Se as failures persistem no baseline sem o diff = **pré-existentes, NÃO bloqueantes** (APPROVE com nota); só introduzidas pela PR = REQUEST_CHANGES.
- **Quando sou autor+assignee (PR própria, sou o time):** zero regressão funcional + suíte verde → **MERGEAR DIRETO** (`{merge_cmd}` — não dá pra aprovar formalmente a própria PR; o merge É o veredito). ACs abertos só de doc/qualidade NÃO bloqueiam o merge → mergeie e abra UMA sub-issue de follow-up agregada (anti-flood) com o checklist. Só NÃO mergeie se há bug funcional, regressão real ou requisito CORE não atendido — aí grave o achado e o PRÓXIMO tick implementa (não re-comenta).
- Se PR fecha issue (`Closes #N` no body): leia `gh issue view N` e confronte entrega vs requisito.
- Se PR toca `briefs.py`/`*_brief*.py`: rode `pytest deile/tests/orchestration/pipeline/test_briefs* -v` E `grep -E 'CHECKPOINT|SUÍTE COMPLETA|anti-eco|gh pr comment|.deile-progress.md' deile/orchestration/pipeline/briefs.py` — invariantes ausentes = REQUEST_CHANGES cirúrgico.

**PASSO 3 — DECISÃO É DECISÃO** (anti-loop, regra inegociável):
- TEM conclusão → poste review formal e ENCERRE com veredito. NÃO escreva "incompleto" se chegou a decidir.
- Work-list ESVAZIOU → pipeline marca `~mention:processado`.
- CHECKPOINT INCREMENTAL: grave/atualize `.deile-progress.md` (um nível acima de ./repo, NÃO commite, NÃO dentro de ./repo) **a cada milestone concluído — não espere o fim**. O timeout do dispatch MATA o processo sem aviso (rc=124); só o que estiver no journal sobrevive pro próximo tick. Escreva-o como um PRE-COMPACT: feito, falta, decisões, bloqueio, arquivo:linha.

PASSO 4 — ÚLTIMA LINHA:
- `{pr_url_pattern} MERGED` — mergeado de fato (`{check_merged_cmd}`).
- `{pr_url_pattern} REVIEWED:APPROVED` — review APPROVE postada.
- `{pr_url_pattern} REVIEWED:CHANGES` — review REQUEST_CHANGES postada.
- `{pr_url_pattern} COMMENTED` — só comentário, sem review formal. **PROIBIDO** como veredito final de PR PRÓPRIA com suíte verde: nesse caso o correto é `MERGED`.
- `{blocked_contract}` — impedimento real.
NUNCA invente resultado. NUNCA termine sem veredito quando TEM decisão.
"""


# --- Resume briefs (issue #254) -----------------------------------------------
# The fundamental difference from the fresh briefs: NO ``git reset --hard``. The
# branch and the untracked files in the persistent per-channel workspace are the
# partial work from a previous attempt and MUST be preserved. The brief injects
# the journal (.deile-progress.md), the current diff and tells the agent to read
# every untracked file so it continues with the SAME context, then write an
# updated journal before it stops.

_WORKER_IMPLEMENT_RESUME_BRIEF = """\
RETOMADA da issue #{number} de {repo}. Há trabalho parcial em ./repo — NÃO recomece. NÃO rode `git reset --hard`. NÃO apague untracked. `.deile-progress.md` é INTOCÁVEL — leia, atualize, NUNCA delete.

1. `git checkout {branch}` (existe localmente).
2. Reconstrua contexto:
{progress_block}
   `git diff {main}...HEAD` + `git diff HEAD` + `git status --porcelain` (leia cada arquivo listado).
3. Continue de onde parou. Edite o que falta + testes.
4. {impact_test_strategy}
5. {dod_block}
6. Commit + `git push -u origin {branch}`.
7. Abra a {pr_noun} (OBRIGATÓRIO): `{create_pr_cmd}` (ou confirme existente: `{check_pr_cmd}`). No --body, confronte ENTREGA vs cada AC (com números).
8. CHECKPOINT INCREMENTAL: atualize `.deile-progress.md` no diretório de trabalho (NÃO commite, NÃO em ./repo) **a cada milestone E antes de parar** — o timeout mata o processo sem aviso (rc=124), só o journal sobrevive pro próximo tick: feito, falta, decisões, bloqueios.
9. Impedimento real → linha `{blocked_contract}`. Última linha = URL da {pr_noun} (ex: {pr_url_pattern}) OU `{blocked_contract}`.

=== Issue #{number}: {title} ===
{body}
"""

# --- Address-review-feedback brief (Fix #8 — issue #521) ----------------------
# Despachado quando a review da NOSSA PRÓPRIA PR concluiu REQUEST_CHANGES e o
# HEAD não mudou desde a última review (nenhum fix aplicado). Em vez de bloquear
# direto, o pipeline manda UMA task de IMPLEMENT na branch da PR que APLICA o que
# o reviewer pediu e dá PUSH — explicitamente NÃO revisa, NÃO comenta veredito,
# NÃO mergeia. Quando o push muda o HEAD, a próxima review valida o novo HEAD e
# segue pro merge. Lean de propósito: o worker LÊ a última review via gh (o
# feedback exato vive lá), evitando que o pipeline tenha de capturar e reembalar
# o corpo do REQUEST_CHANGES.
_WORKER_PR_ADDRESS_BRIEF = """\
APLIQUE as mudanças que o reviewer pediu na {pr_noun} #{number} de {repo} e dê PUSH. NÃO revise, NÃO comente veredito, NÃO mergeie — só CORRIJA o código.

A review anterior concluiu REQUEST_CHANGES e o HEAD não mudou — nenhum fix foi aplicado ainda. Sua única tarefa é IMPLEMENTAR o pedido do reviewer.

1. Clone se preciso ({clone_cmd}); `{checkout_pr_cmd}` (trabalhe na branch da própria {pr_noun}, NÃO crie branch nova).
2. Leia a ÚLTIMA review REQUEST_CHANGES e os comentários: `{list_pr_comments_cmd}` e `gh api repos/{repo}/pulls/{number}/reviews --jq '[.[] | select(.state=="CHANGES_REQUESTED")] | last | .body'`. Esse é o escopo exato do que corrigir.
3. Leia `.deile-progress.md` no diretório de trabalho (um nível acima de ./repo) — TODO da tentativa anterior, se houver.
4. APLIQUE a correção no código + testes. Se um achado for genuinamente fora-de-escopo/inválido, registre o porquê no `.deile-progress.md` (não re-comente na PR).
5. {impact_test_strategy}
6. Commit atômico + `git push -u origin {branch}` (PUSH é OBRIGATÓRIO — sem push o HEAD não muda e a {pr_noun} fica bloqueada). NÃO force-push. NÃO altere nada fora de ./repo.
7. Impedimento real (não consegue aplicar o fix) → linha `{blocked_contract}`.

ÚLTIMA LINHA = SHA do novo HEAD após o push (`git rev-parse HEAD`) OU `{blocked_contract}`.
"""

_WORKER_MENTION_BRIEF = """\
Acionado por {trigger_summary} em {repo}.

Gatilhos:
{trigger_details}

Ação:
{expected_action}

Regras:
- ASSIGNEE em issue sem {pr_noun} → implemente (branch + commit + testes + {pr_noun}).
- ASSIGNEE em issue com {pr_noun} → checkout branch da {pr_noun} e continue se faltar algo.
- REQUESTED REVIEWER → review (SOLID/DRY/KISS) + testes + corrigir + push + comentar evidências + merge.
- MENTION em comentário → responda ao pedido; se for em {pr_noun}, trabalhe no branch dela.
- Sempre poste resposta/evidências como comentário ({forge_name}). ÚLTIMA LINHA = URL relevante.
"""

# Shared mention-rendering helpers (issue #253). The worker brief and the Claude
# prompt describe the same triggers and pick the same action by role; they differ
# only in surface formatting (markdown vs prose) and wording. Centralizing the
# trigger-type labels, the trigger-detail iteration and the action classification
# keeps the two renderers from drifting apart.
_MENTION_TYPE_LABELS = {
    "assignee": "assignee (atribuído a você)",
    "reviewer": "reviewer (solicitado como revisor)",
    "comment": "menção em comentário (@deile-one)",
    "body": "menção no corpo (@deile-one)",
}


def _summarize_trigger_types(trigger_types: list[str]) -> str:
    return " + ".join(_MENTION_TYPE_LABELS.get(t, t) for t in trigger_types)


def _render_trigger_details(all_triggers: list["MentionTrigger"], *, rich: bool) -> str:
    """Render one bullet per trigger. ``rich=True`` emits markdown (bold labels,
    ``[title](url)`` links, fenced comment bodies); ``rich=False`` emits plain prose."""

    def label(text: str) -> str:
        return f"**{text}**" if rich else text

    def link(title: str, url: str) -> str:
        return f"[{title}]({url})" if rich else f"{title} ({url})"

    parts: list[str] = []
    for t in all_triggers:
        if t.trigger_type == "comment" and t.comment is not None:
            body = t.comment.body[:500]
            body_block = f"\n  ```\n  {body}\n  ```" if rich else f"\n  {body}"
            parts.append(
                f"- {label('Comentário')} de @{t.comment.author} em {t.comment.html_url}:{body_block}"
            )
        elif t.trigger_type == "assignee" and t.issue is not None:
            parts.append(
                f"- {label('Assignado')} na issue #{t.issue.number}: {link(t.issue.title, t.issue.url)}"
            )
        elif t.trigger_type == "assignee" and t.pr is not None:
            parts.append(
                f"- {label('Assignado')} na PR #{t.pr.number}: {link(t.pr.title, t.pr.url)}"
            )
        elif t.trigger_type == "reviewer" and t.pr is not None:
            parts.append(
                f"- {label('Solicitado como reviewer')} na PR #{t.pr.number}: {link(t.pr.title, t.pr.url)}"
            )
        elif t.trigger_type == "body":
            if t.issue is not None:
                parts.append(
                    f"- {label('Menção no corpo')} da issue #{t.issue.number}: {link(t.issue.title, t.issue.url)}"
                )
            elif t.pr is not None:
                parts.append(
                    f"- {label('Menção no corpo')} da PR #{t.pr.number}: {link(t.pr.title, t.pr.url)}"
                )
    return "\n".join(parts)


def _classify_mention_action(ref: "MentionTrigger", trigger_types: list[str]) -> str:
    """Map the trigger set + target kind to a canonical action key. Both renderers
    branch on this key for their own wording."""
    target_kind = ref.target_kind
    has_assignee = "assignee" in trigger_types
    has_reviewer = "reviewer" in trigger_types
    has_comment = "comment" in trigger_types
    has_body = "body" in trigger_types

    if has_reviewer and target_kind == "pr":
        return "review_request"
    if has_assignee and target_kind == "issue":
        return "assigned_issue"
    if has_assignee and target_kind == "pr":
        return "assigned_pr"
    if has_comment or has_body:
        return "mention_pr" if target_kind == "pr" else "mention_issue"
    return "default"


# ---------------------------------------------------------------------------
# Unified mention-action templates (DRY across worker + claude renderers).
#
# Both `_render_worker_mention_brief` (markdown) and `_render_claude_mention_prompt`
# (plain prose) used to carry near-identical 6-key `actions = {...}` dicts that
# differed only in surface formatting. Wording drift had already produced subtle
# divergence between them. Centralising the bodies here means one wording change
# touches one place.
#
# Forge-agnostic (issue #297): the bodies use ``{pr_noun}`` and ``{label_pr}``
# placeholders so the same template renders "PR" (GitHub) or "MR" (GitLab) at
# call time. Each entry has a LABEL (the role/action header, e.g. "REVIEW
# REQUEST" or "ASSIGNED TO {label_pr}") and a BODY (the instruction text after
# the label). The two formatters below decorate them differently:
# rich → ``**REVIEW REQUEST**: <body>``; plain → ``REVIEW REQUEST: <body>``.
# ``default`` has no label — only a body.
# ---------------------------------------------------------------------------
_MENTION_ACTION_TEMPLATES: dict[str, tuple[str | None, str]] = {
    "review_request": (
        "REVIEW REQUEST",
        "Você foi solicitado como revisor da {pr_noun} #{n}. "
        "Faça uma revisão completa (arquitetura, DRY, KISS, SOLID, clean code), "
        "rode os testes, corrija problemas, faça commit + push, poste evidências como "
        "comentário na {pr_noun} e faça o merge.",
    ),
    "assigned_issue": (
        "ASSIGNED",
        "Você foi atribuído à issue #{n}. "
        "Implemente a feature completa, crie testes, abra uma {pr_noun}. "
        "Se já existir uma {pr_noun} para esta issue, verifique se cobre tudo e "
        "continue a implementação no branch existente.",
    ),
    "assigned_pr": (
        "ASSIGNED TO {label_pr}",
        "Você foi atribuído à {pr_noun} #{n}. "
        "Revise, corrija, teste, faça commit + push e mergeie se estiver pronto.",
    ),
    "mention_pr": (
        "MENTION ON {label_pr}",
        "Você foi mencionado na {pr_noun} #{n}. "
        "Atenda ao que foi pedido no comentário/corpo, trabalhe no branch da {pr_noun}, "
        "teste, faça commit + push e poste a resposta como comentário na {pr_noun}.",
    ),
    "mention_issue": (
        "MENTION ON ISSUE",
        "Você foi mencionado na issue #{n}. "
        "Atenda ao que foi pedido no comentário/corpo. "
        "Se a issue já tem {pr_noun} aberta, trabalhe no branch da {pr_noun}.",
    ),
    "default": (None, "Atenda ao contexto acima da forma mais apropriada."),
}


def _format_mention_action(action: str, n: int, pr_noun: str = "PR", *, rich: bool) -> str:
    """Render a mention action label + body. ``rich=True`` wraps the label in
    markdown bold (worker brief style); ``rich=False`` emits plain prose
    (Claude prompt style)."""
    label, body = _MENTION_ACTION_TEMPLATES[action]
    body = body.format(n=n, pr_noun=pr_noun, label_pr=pr_noun)
    if label is None:
        return body
    rendered_label = label.format(label_pr=pr_noun)
    return f"**{rendered_label}**: {body}" if rich else f"{rendered_label}: {body}"


def _format_mention_action_rich(action: str, n: int, pr_noun: str = "PR") -> str:
    """Render a mention action with markdown bold label (worker brief style)."""
    return _format_mention_action(action, n, pr_noun, rich=True)


def _format_mention_action_plain(action: str, n: int, pr_noun: str = "PR") -> str:
    """Render a mention action with plain prose label (Claude prompt style)."""
    return _format_mention_action(action, n, pr_noun, rich=False)


# Context-aware worker mention brief builder (issue #253)
def _render_worker_mention_brief(
    repo: str,
    ref: "MentionTrigger",
    trigger_types: list[str],
    all_triggers: list["MentionTrigger"],
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    """Build a context-rich mention brief from all trigger types."""
    trigger_summary = _summarize_trigger_types(trigger_types)
    trigger_details = _render_trigger_details(all_triggers, rich=True)

    cfg = forge or _default_forge_config(repo)
    pr_noun = "PR" if cfg.kind is ForgeKind.GITHUB else "MR"
    forge_name = "GitHub" if cfg.kind is ForgeKind.GITHUB else "GitLab"
    expected_action = _format_mention_action_rich(
        _classify_mention_action(ref, trigger_types),
        ref.target_number,
        pr_noun=pr_noun,
    )

    return _WORKER_MENTION_BRIEF.format(
        repo=repo,
        trigger_summary=trigger_summary,
        trigger_details=trigger_details,
        expected_action=expected_action,
        pr_noun=pr_noun,
        forge_name=forge_name,
    )


def _render_worker_implement_brief(
    repo: str,
    main: str,
    branch: str,
    number: int,
    title: str,
    body: str,
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    params = _build_brief_params(
        repo=repo, main=main, branch=branch, number=number, forge=forge,
        close_keyword="Refs" if _is_spike(title, body) else "Closes",
        extras={
            "title": title,
            "body": (body or "").strip()[:ISSUE_BODY_MAX_CHARS]
                    or "(sem corpo — implemente a partir do título)",
        },
    )
    return _render_brief(_WORKER_IMPLEMENT_BRIEF, params=params)


def _render_worker_pr_unified_brief(
    repo: str,
    main: str,
    number: int,
    *,
    gh_login: str,
    forge: Optional[ForgeConfig] = None,
) -> str:
    """Renderiza o brief unificado de PR (refactor "PR é o quadro").

    Substitui ``_render_worker_review_brief``, ``_render_worker_review_only_brief``,
    ``_render_worker_pr_address_brief`` e ``_render_worker_review_resume_brief``.
    O worker monta a work-list a partir do estado real da PR (papel, HEAD vs
    último review, threads abertas, comments dirigidos a mim sem resposta) —
    NÃO do trigger. Resume é gratuito: o passo 0 instrui ler
    ``.deile-progress.md``.

    ``gh_login`` é o handle do agente (ex: ``deile-one``) sem o ``@`` — usado
    para o worker comparar com ``.user.login`` em reviews/assignees/reviewers
    e detectar comments dirigidos a si próprio.
    """
    branch_for_render = f"pr/{number}"
    params = _build_brief_params(
        repo=repo, main=main, branch=branch_for_render, number=number, forge=forge,
        extras={"gh_login": gh_login},
    )
    return _render_brief(_WORKER_PR_BRIEF, params=params)


def _render_worker_pr_address_brief(
    repo: str,
    main: str,
    branch: str,
    number: int,
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    """Renderiza o brief de address-review-feedback (Fix #8 — issue #521).

    Lean de propósito: o worker lê a última review REQUEST_CHANGES via ``gh`` (o
    feedback exato vive lá) e APLICA o fix + push na branch da própria PR. NÃO
    revisa, NÃO comenta veredito, NÃO mergeia — o ciclo "pediu-mudança → próximo
    tick IMPLEMENTA" da Decisão #46 materializado num dispatch dedicado.
    """
    params = _build_brief_params(
        repo=repo, main=main, branch=branch, number=number, forge=forge,
    )
    return _render_brief(_WORKER_PR_ADDRESS_BRIEF, params=params)


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
    repo: str,
    main: str,
    branch: str,
    number: int,
    title: str,
    body: str,
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    params = _build_brief_params(
        repo=repo, main=main, branch=branch, number=number, forge=forge,
        close_keyword="Refs" if _is_spike(title, body) else "Closes",
        extras={
            "title": title,
            "body": (body or "").strip()[:ISSUE_BODY_MAX_CHARS]
                    or "(sem corpo — continue a partir do título e do trabalho parcial)",
            "progress_block": _PROGRESS_BLOCK,
        },
    )
    return _render_brief(_WORKER_IMPLEMENT_RESUME_BRIEF, params=params)


# --- Refinement gate briefs (issue #257) --------------------------------------
# Three briefs that run BEFORE any implementation: CRITIQUE (judge scope),
# REFINE (rewrite the body toward the template, possibly pausing for the
# stakeholder) and DECOMPOSE (an architect splits a clear intent into independent
# derived issues). The persona is chosen by issue type at dispatch time (analyst
# for intent, architect for feature/refactor, debugger for bug) — so the brief
# stays generic and the type-specific JUDGMENT lives in the persona instruction.
# Each brief ends with a strict last-line VERDICT the pipeline parses.

_WORKER_CRITIQUE_BRIEF = """\
GATE DE CRÍTICA da issue #{number} (tipo: {type}) de {repo}. NÃO implemente. Apenas JULGUE se o escopo está claro.

1. Leia issue + comentários + linked: `{view_issue_cmd}` (template: `{fetch_template_cmd}`; clone se preciso: `{clone_cmd}`). Para bug: localize origem `arquivo:linha`.
2. CLARO só se TODOS:
   a) Segue template, sem placeholder vazio.
   b) Critérios de aceite MENSURÁVEIS (número/condição testável; "bem/robusto/performático" = vago).
   b2) Feature de INTEGRAÇÃO (liga/registra/despacha algo no fluxo — novo call-site, wiring em tick/loop, registro em registry, endpoint) EXIGE ≥1 AC de teste de INTEGRAÇÃO provando a FIAÇÃO (o caminho real chama/usa o código), não só unit da função isolada. "Função implementada e testada" sem call-site exercitado NÃO é aceitável (lei de Goodhart — caso GC #596).
   c) Sem promessas vazias ("trivial X depois") sem mecanismo concreto (teste/lint/schema/sub-issue de follow-ups).
   d) Sem lacunas arquiteturais óbvias (idempotência, TOCTOU, timeouts, observabilidade, rollback, threat model, schema migration, SLO, dependências externas).
   e) V1 vs roadmap EXPLÍCITO — o que entra agora vs item em UMA sub-issue agregada (anti-flood).
3. Se VAGO, liste 3-5 defeitos por impacto. Em dúvida → VAGO.

ÚLTIMA LINHA (somente uma):
  VEREDITO: CLARO
  VEREDITO: VAGO: <o que falta>

=== Issue #{number} (tipo: {type}): {title} ===
{body}
"""

_WORKER_REFINE_BRIEF = """\
REFINE a issue #{number} (tipo: {type}) de {repo}. Reescreva título + body. NÃO codifique.

1. Leia issue + comentários + linked + template: `{view_issue_cmd}` / `{fetch_template_cmd}` (clone se preciso: `{clone_cmd}`).

2. Diagnóstico (ordem 2a→2f):
   a) Promessas vazias ("trivial X depois", "alguém edita") → substitua por mecanismo concreto (AC, teste, lint, schema, item no checklist da sub-issue de follow-ups) ou declare fora-de-escopo.
   b) Lacunas arquiteturais (idempotência, TOCTOU, timeouts, observabilidade, rollback, threat model, SLO, schema migration, dependências externas) → resolva, marque N/A com motivo, ou jogue na sub-issue de follow-ups.
   c) V1 vs roadmap: o que adia vira skeleton/DISABLED no código OU item em UMA sub-issue agregada de follow-ups (`- [ ]` por item).
   d) Spinoffs laterais entram no checklist da MESMA sub-issue (não vira sub-issue separada).
   e) ACs DUROS — número, percentual, condição testável. Cobre comportamento + falhas de 2b + decisões de 2c. Para feature de INTEGRAÇÃO (liga/registra/despacha algo no fluxo — novo call-site, wiring em tick/loop, registro em registry, endpoint) inclua ≥1 AC de teste de INTEGRAÇÃO que prove a FIAÇÃO (o caminho real chama/usa o código), não só unit da função isolada — "função implementada e testada" sem call-site exercitado NÃO basta (lei de Goodhart — caso GC #596).
   f) Testes: paths concretos + o que cada um prova.

3. APLIQUE — REGRA ANTI-FLOOD: MÁXIMO UMA sub-issue agregada de follow-ups por issue-mãe. Split só com JUSTIFICATIVA explícita (módulos disjuntos, testes diferentes, ordem livre). Default: agregar.
   - Título prefixo `{title_prefix}`: `{edit_issue_title_cmd}`
   - Body reescrito: `{edit_issue_body_cmd}`
   - Sub-issue de follow-ups (se houver adiamento): `{create_issue_cmd}` com `Originada de #{number}`.
   - Auditoria em `{comment_issue_cmd}`: gaps resolvidos, link da sub-issue, ACs duros, justificativa de split (se houver), "Pronto para implementação" ou "Bloqueado por: <X>".

4. AGUARDA_STAKEHOLDER — só para decisão de PRODUTO de alto impacto. 2-3 sugestões + autor (`{view_pr_author_cmd_for_issue}` + `{assign_user_cmd}`).

5. Cite `arquivo:linha` do que leu. Só liste sub-issue REALMENTE aberta.

ÚLTIMA LINHA (somente uma):
  REFINO: OK
  REFINO: AGUARDA_STAKEHOLDER

=== Issue #{number} (tipo: {type}): {title} ===
{body}
"""

_WORKER_DECOMPOSE_BRIEF = """\
ARQUITETO. Intent #{number} de {repo} está CLARA. Decomponha em derivadas INDEPENDENTES, no padrão de maestria.

REGRA ANTI-FLOOD (V1 inegociável — auditável em PR #466): cada derivada custa refine+critique+implement+review (~$3-30 por ciclo). DEFAULT AGRESSIVO: AGREGAR em UMA derivada com checklist (`- [ ]` por item). Split SÓ se cada derivada tem PR independente (módulos disjuntos, testes diferentes, ordem livre). Em dúvida: agregar. Justifique split no comment de auditoria.

Exemplo:
  BOM (default):      "4 frentes A/B/C/D no módulo M → UMA derivada com checklist."
  SPLIT JUSTIFICADO:  "A/B no módulo M, C/D no módulo N (disjuntos) → derivada #X e #Y."

Algoritmo:
1. Ancore na arquitetura real (`docs/system_design/`, código; clone: `{clone_cmd}`).
2. Liste items de escopo → DEFAULT: UMA derivada com checklist. Split só com prova de independência.
3. Cada derivada nasce no padrão (sem refino depois):
   - título com prefixo do tipo; body conforme template (`.github/ISSUE_TEMPLATE/*` ou `.gitlab/issue_templates/*`).
   - alvo técnico, contrato, ACs MENSURÁVEIS, paths de teste. Derivada de INTEGRAÇÃO (novo call-site, wiring em tick/loop, registro em registry, endpoint) carrega ≥1 AC de teste de INTEGRAÇÃO provando a FIAÇÃO — não só unit isolado (lei de Goodhart — caso GC #596).
   - checklist `- [ ]` por item; lacunas arquiteturais pertinentes endereçadas; V1 vs roadmap explícito.
   - linha `Originada de #{number}`. Comando: `{create_issue_cmd}`.
4. Audit na intent: `{comment_issue_cmd}` com derivadas criadas, ordem de dependência, justificativa do split se houver.

ÚLTIMA LINHA:
  DECOMPOSTO: #<n1> #<n2> ...

=== Intent #{number}: {title} ===
{body}
"""


def _refine_body(body: str) -> str:
    return (body or "").strip()[:ISSUE_BODY_MAX_CHARS] or "(sem corpo — avalie a partir do título)"


def _view_issue_author_cmd(cfg: ForgeConfig, repo: str, number: int) -> str:
    """Per-forge command to fetch the author handle of an issue.

    ``render_brief_cmds`` exposes ``view_pr_author_cmd`` (assumes a PR target);
    the refine brief needs the same query against an issue, with the GitLab
    branch using the same project-id resolution the other ``glab`` snippets do.
    """
    if cfg.kind is ForgeKind.GITHUB:
        return f"gh issue view {number} --repo {repo} --json author -q .author.login"
    return (
        f"glab api projects/{cfg.project_id or cfg.encoded_project_path}"
        f"/issues/{number} | jq -r .author.username"
    )


def _render_worker_critique_brief(
    repo: str,
    number: int,
    title: str,
    body: str,
    *,
    issue_type: str,
    template: str,
    forge: Optional[ForgeConfig] = None,
) -> str:
    params = _build_brief_params(
        repo=repo, main="main", branch=f"refine-{number}", number=number,
        forge=forge, issue_template=template,
        extras={
            "title": title,
            "body": _refine_body(body),
            "type": issue_type,
        },
    )
    return _render_brief(_WORKER_CRITIQUE_BRIEF, params=params)


def _render_worker_refine_brief(
    repo: str,
    number: int,
    title: str,
    body: str,
    *,
    issue_type: str,
    template: str,
    forge: Optional[ForgeConfig] = None,
) -> str:
    # _build_brief_params already resolves the default; we only need the
    # concrete ForgeConfig for the per-forge author-lookup snippet below.
    cfg_for_author = forge or _default_forge_config(repo)
    params = _build_brief_params(
        repo=repo, main="main", branch=f"refine-{number}", number=number,
        forge=forge, issue_template=template,
        extras={
            "title": title,
            "body": _refine_body(body),
            "type": issue_type,
            "title_prefix": title_prefix_for_type(issue_type) or "[FEATURE]",
            "view_pr_author_cmd_for_issue": _view_issue_author_cmd(
                cfg_for_author, repo, number,
            ),
        },
    )
    return _render_brief(_WORKER_REFINE_BRIEF, params=params)


def _render_worker_decompose_brief(
    repo: str,
    number: int,
    title: str,
    body: str,
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    params = _build_brief_params(
        repo=repo, main="main", branch=f"decompose-{number}", number=number,
        forge=forge,
        extras={
            "title": title,
            "body": _refine_body(body),
        },
    )
    return _render_brief(_WORKER_DECOMPOSE_BRIEF, params=params)


# ---------------------------------------------------------------------------
# Claude mention prompt builder (issue #253)
# ---------------------------------------------------------------------------

def _render_claude_mention_prompt(
    repo: str,
    ref: "MentionTrigger",
    trigger_types: list[str],
    all_triggers: list["MentionTrigger"],
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    """Build a context-rich mention prompt for the Claude path."""
    trigger_summary = _summarize_trigger_types(trigger_types)
    trigger_details = _render_trigger_details(all_triggers, rich=False)
    cfg = forge or _default_forge_config(repo)
    pr_noun = "PR" if cfg.kind is ForgeKind.GITHUB else "MR"
    forge_cli = cfg.cli_path.rsplit("/", 1)[-1] if cfg.cli_path else (
        "gh" if cfg.kind is ForgeKind.GITHUB else "glab"
    )

    action = _format_mention_action_plain(
        _classify_mention_action(ref, trigger_types),
        ref.target_number,
        pr_noun=pr_noun,
    )

    return (
        f"Você foi acionado por {trigger_summary} no repositório {repo}.\n\n"
        f"Contexto:\n{trigger_details}\n\n"
        f"Ação esperada:\n{action}\n\n"
        f"IMPORTANTE: Poste a resposta como comentário no repositório usando {forge_cli}. "
        f"Na última linha, escreva a URL relevante."
    )
