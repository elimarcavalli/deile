"""Worker/Claude brief templates and renderers for the autonomous pipeline.

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
    "TESTES — análise de impacto (NÃO rode a suíte completa; isso é tarefa do revisor):\n"
    "   a) Liste os arquivos que você criou/editou:\n"
    "        git diff --name-only HEAD\n"
    "   b) Para CADA arquivo `deile/X/Y/z.py` editado, identifique os testes impactados:\n"
    "      • Testes diretos (mesmo módulo/subpacote):\n"
    "          ls deile/tests/X/Y/test_z.py deile/tests/X/Y/test_*.py 2>/dev/null\n"
    "      • Testes que importam o módulo editado:\n"
    '          grep -rln "from deile.X.Y" deile/tests/ 2>/dev/null\n'
    "      • Se tocou `deile/orchestration/pipeline/` → inclua `deile/tests/orchestration/pipeline/`\n"
    "      • Se tocou `deile/orchestration/forge/` → inclua `deile/tests/orchestration/pipeline/`\n"
    "      • Se tocou `deile/tools/` → inclua `deile/tests/tools/`\n"
    "      • Se tocou `deile/core/` ou `deile/config/` → inclua testes de cada subpacote afetado\n"
    "      • Se tocou `infra/k8s/` (ex: claude_worker_server.py) → inclua `deile/tests/infrastructure/`\n"
    "   c) Construa a lista completa e rode: `python3 -m pytest <lista_de_testes_impactados> -p no:cov -q`\n"
    "      " + _PIP_GUARD + ". Itere até verde.\n"
    "   d) Se a lista impactada for vazia (ex: mudou só um YAML de config ou Markdown):\n"
    "      rode apenas os testes do módulo mais próximo como sanity-check.\n"
    "   NUNCA rode `" + _FULL_SUITE_CMD + "` (suíte inteira) — o revisor fará isso no gate de merge."
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


def _build_brief_params(
    *,
    repo: str,
    main: str,
    branch: str,
    number: int,
    forge: Optional[ForgeConfig],
    issue_template: str = "feature_request.md",
    extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble the full ``{key: value}`` map for ``.format()`` on a template.

    Combines: the shared constants (test command, pip guard, BLOQUEADO),
    the runtime values (``repo``/``main``/``branch``/``number``) and the
    per-forge CLI snippets from :func:`render_brief_cmds`. Extras override
    everything else so callers can inject brief-specific fields
    (``title``/``body``/``progress_block`` …) without surprises.
    """
    cfg = forge or _default_forge_config(repo)
    cmds = render_brief_cmds(
        cfg, number=number, branch=branch, main=main, issue_template=issue_template,
    )
    params: dict[str, Any] = dict(_BRIEF_CONSTANTS)
    params.update({
        "repo": repo,
        "main": main,
        "branch": branch,
        "number": number,
    })
    params.update(cmds)
    if extras:
        params.update(extras)
    return params


def _render_brief(template: str, *, params: dict[str, Any]) -> str:
    """Format a brief template with a pre-built params dict."""
    return template.format(**params)


def _inject_view_issue_template(params: dict[str, Any], number: int) -> None:
    """Add ``view_issue_cmd_template`` (the cmd with ``<N>`` placeholder).

    The review briefs reference ``gh issue view <N> --comments`` where ``<N>``
    is the issue closed by the PR — unknown at brief-render time. Replace the
    concrete number in ``view_issue_cmd`` with the ``<N>`` literal so the
    worker fills it in after parsing the closing reference.
    """
    params["view_issue_cmd_template"] = params["view_issue_cmd"].replace(
        f" {number} ", " <N> ", 1,
    )


# ---------------------------------------------------------------------------
# Implement / review briefs
# ---------------------------------------------------------------------------


_WORKER_IMPLEMENT_BRIEF = """\
Implemente a issue #{number} do repositório {repo} e abra uma {pr_noun} — execute de verdade, não simule nem invente.

Passo a passo:
1. Trabalhe na subpasta ./repo do seu diretório atual. Se ./repo não existir, rode: {clone_cmd}
   Se já existir, entre nela e rode: git fetch origin && git checkout {main} && git reset --hard origin/{main}
2. Dentro de ./repo, crie e dê checkout no branch {branch} a partir de {main}.
3. ANTES de codar, leia os comentários da issue: {view_issue_cmd} — decisões e esclarecimentos do stakeholder ali FAZEM PARTE do escopo (o corpo pode não conter tudo). Implemente a feature descrita na issue abaixo E o que os comentários decidiram. Crie/edite os arquivos necessários e ADICIONE testes cobrindo todos os casos.
4. {impact_test_strategy}
5. Faça commit atômico e `git push -u origin {branch}`.
6. ABRA A {pr_noun} (passo OBRIGATÓRIO — sem {pr_noun} a tarefa NÃO está concluída):
   {create_pr_cmd}
7. CONFIRME que a {pr_noun} existe antes de responder:
   {check_pr_cmd}
   (se não retornar URL, a {pr_noun} NÃO foi criada — volte ao passo 6 e crie de fato.)
8. NÃO faça force-push. NÃO altere nada fora de ./repo.
9. Na ÚLTIMA LINHA da resposta final, escreva SOMENTE a URL da {pr_noun} confirmada no passo 7 (ex.: {pr_url_pattern}). Nada depois dela.

DEFINITION OF DONE: existe uma {pr_noun} aberta cuja URL você confirmou via {forge_cli}. Se push/{forge_cli}/testes falharem, reporte o erro REAL — NUNCA invente uma URL nem diga "concluído" sem a {pr_noun} existir.

=== Issue #{number}: {title} ===
{body}
"""

# --- Unified PR brief (refactor "PR é o quadro") ------------------------------
# Substitui ``_WORKER_REVIEW_BRIEF``, ``_WORKER_REVIEW_ONLY_BRIEF`` e
# ``_WORKER_PR_ADDRESS_BRIEF``. O princípio inegociável: a PR é o quadro; o
# trigger só diz QUAL PR olhar. O worker abre a PR, descobre o estado real e
# monta a work-list a partir DELE — não do trigger que o acordou. Resume é
# coberto pelo PASSO 0 lendo ``.deile-progress.md``; merge só acontece quando
# o autor sou eu E meu review atual está APPROVED E threads ok E suíte verde.
_WORKER_PR_BRIEF = """\
Você é membro do time de desenvolvimento do {repo}. Foi acordado por algum trigger na {pr_noun} #{number}. NÃO IMPORTA qual trigger — abra a {pr_noun} e descubra o que precisa ser feito a partir do ESTADO REAL dela.

CHECKPOINT OBRIGATÓRIO (vale pra QUALQUER worker): a execução é FALHA se você terminar sem ter postado pelo menos UM comentário visível na {pr_noun} via {comment_pr_cmd}.

PASSO 0 — DESCOBERTA DE ESTADO (não pule):
1. Clone se preciso ({clone_cmd}), faça checkout: {checkout_pr_cmd}.
2. Se existir `.deile-progress.md` no SEU diretório de trabalho (um nível acima de ./repo), LEIA-O — é a sua TODO da tentativa anterior. Continue de onde parou.
3. Leia o ESTADO da {pr_noun} via REST:
   gh api repos/{repo}/pulls/{number} --jq '{{author:.user.login, assignees:[.assignees[].login], requested_reviewers:[.requested_reviewers[].login], head_sha:.head.sha, mergeable:.mergeable}}'
   gh api repos/{repo}/pulls/{number}/reviews --jq '[.[] | select(.user.login == "{gh_login}")] | sort_by(.submitted_at) | last'
   gh api repos/{repo}/issues/{number}/comments --jq '.[] | {{author:.user.login, body, created_at}}'
4. Calcule:
   - meu_papel = {{{{autor: author == "{gh_login}", assignee: "{gh_login}" in assignees, reviewer: "{gh_login}" in requested_reviewers}}}}
   - meu_review_atual = último review meu cujo commit_id == head_sha (None se nenhum)
   - threads_abertas = threads de review sem resposta sua nem resolvidas
   - comments_dirigidos_a_mim_sem_resposta = comments contendo "@{gh_login}" (humano OU auto) cuja última reply do thread NÃO é minha (tem que ter consciência dos dois — seu próprio comment auto-mencionado conta se ainda não foi atendido)

PASSO 1 — MONTE SUA WORK-LIST a partir do estado (não dos triggers):
□ HEAD mudou desde meu_review_atual E sou reviewer/assignee → revisar
□ thread aberta dirigida a mim → responder/resolver
□ comment dirigido a mim sem resposta → atender o pedido específico
□ sou assignee + meu_review_atual.state == APPROVED + threads ok + CI verde → MERGEAR
□ sou reviewer só, review feita, HEAD igual → comentar curto "já APPROVED em <sha>, sem novidade"

REGRA DE EXECUÇÃO (independente do autor, humano ou eu) — execute o pedido SEMPRE:
- Se a {pr_noun} está OPEN → faça push direto na própria branch da {pr_noun}.
- Se a {pr_noun} está MERGED ou CLOSED → abra uma branch nova derivada (nome sugerido: `auto/<branch-original>-followup-<short-sha>`), atualizada com {main}, execute o pedido, abra uma NOVA {pr_noun} mencionando a anterior por número (`Closes #{number} follow-up`).
- NUNCA deixe um pedido pendente nem ignore um trigger só porque o autor é outro humano — você é membro do time.

PASSO 2 — EXECUTAR a work-list em ordem.
- NÃO se auto-mencione no corpo de NENHUM comment/review (anti-eco — sua identidade é deduzida do .user.login).
- Para review: use {review_post_cmd} com APPROVE ou REQUEST_CHANGES.
- Para merge: {merge_cmd} (fallback: {merge_fallback_cmd}). Confirme: {check_merged_cmd}.
- Para comment: {comment_pr_cmd}.
- Gate de testes: se for revisar ou mergear, rode a SUÍTE COMPLETA: {full_suite_cmd} ({pip_guard}). Suíte vermelha por culpa da sua mudança = bloqueante. Suíte vermelha por testes pré-existentes que sua mudança NÃO TOCOU = você documenta no comment e segue.
- Confronte entrega contra pedido (passo 2b clássico): leia issue que a {pr_noun} fecha (Closes #N), liste itens, valide entrega.

PASSO 3 — ESCREVA O PROGRESSO:
- Se a work-list ESVAZIOU: marque `~mention:processado` automaticamente (o pipeline faz isso pós-success). Você só precisa garantir os steps acima.
- Se a work-list NÃO esvaziou (estourou tempo/orçamento ou impedimento): grave `.deile-progress.md` no seu diretório de trabalho (NÃO commite, NÃO dentro de ./repo) com: o que fez, o que falta, o que tá bloqueando. Próximo tick reusa.

PASSO 4 — VEREDITO. Na ÚLTIMA LINHA escreva:
- URL da {pr_noun} seguida de MERGED se mergeou (ex.: {pr_url_pattern} MERGED).
- URL da {pr_noun} sozinha se ciclou ok sem mergear.
- `{blocked_contract}` se impedimento real.
NUNCA escreva MERGED sem ter mergeado de fato; NUNCA invente resultado.
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
5. {impact_test_strategy}
6. Faça commit normal (SEM force-push) e `git push -u origin {branch}`.
7. ABRA A {pr_noun} (OBRIGATÓRIO — sem {pr_noun} a tarefa NÃO está concluída):
   {create_pr_cmd}
   (Se já existe uma {pr_noun} para {branch}, apenas confirme-a: {check_pr_cmd})
8. ANTES DE PARAR (concluindo OU pausando de novo), ATUALIZE o journal `.deile-progress.md` no diretório de trabalho (NÃO dentro de ./repo, e NÃO commite): registre o que fez, o que falta, decisões-chave e qualquer bloqueio.
9. Se um IMPEDIMENTO REAL impedir continuar (falta credencial/segredo, dependência impossível, decisão de produto pendente), escreva numa linha começando com `{blocked_contract}` — só você sabe disso; o pipeline respeita isso e para de retomar.
10. Na ÚLTIMA LINHA: a URL da {pr_noun} confirmada (ex.: {pr_url_pattern}), ou, se bloqueado, a linha `{blocked_contract}`. Nada depois dela.

DEFINITION OF DONE: existe uma {pr_noun} aberta cuja URL você confirmou via {forge_cli}. NUNCA invente URL nem diga "concluído" sem a {pr_noun} existir.

=== Issue #{number}: {title} ===
{body}
"""

_WORKER_MENTION_BRIEF = """\
Você foi acionado por {trigger_summary} no repositório {repo}.

Contexto completo dos gatilhos detectados:
{trigger_details}

Ação esperada:
{expected_action}

IMPORTANTE:
- Se for um ASSIGNEE em uma issue SEM {pr_noun} aberta, implemente a issue completa (use o fluxo normal de implementação: branch + commit + teste + {pr_noun}).
- Se for um ASSIGNEE em uma issue que JÁ TEM {pr_noun} aberta, verifique se a {pr_noun} cobre tudo da issue. Se não cobrir, faça checkout do branch da {pr_noun} e continue a implementação.
- Se for REQUESTED REVIEWER, faça review completa (arquitetura, DRY, KISS, SOLID, clean code) + teste + corrija o que precisar + commit + push + comente as evidências + merge.
- Se for MENTION em comentário, responda diretamente ao que foi pedido. Se o comentário está numa {pr_noun}, trabalhe no branch da {pr_noun}.
- Poste SEMPRE a resposta ou evidências como comentário no {forge_name}.
- Na ÚLTIMA LINHA, escreva a URL relevante ({pr_noun}, issue, etc).
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


def _format_mention_action_rich(action: str, n: int, pr_noun: str = "PR") -> str:
    """Render a mention action with markdown bold label (worker brief style)."""
    label, body = _MENTION_ACTION_TEMPLATES[action]
    body = body.format(n=n, pr_noun=pr_noun, label_pr=pr_noun)
    if label is None:
        return body
    return f"**{label.format(label_pr=pr_noun)}**: {body}"


def _format_mention_action_plain(action: str, n: int, pr_noun: str = "PR") -> str:
    """Render a mention action with plain prose label (Claude prompt style)."""
    label, body = _MENTION_ACTION_TEMPLATES[action]
    body = body.format(n=n, pr_noun=pr_noun, label_pr=pr_noun)
    if label is None:
        return body
    return f"{label.format(label_pr=pr_noun)}: {body}"


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
Você é o GATE DE CRÍTICA DE ESCOPO da issue #{number} (tipo: {type}) do repositório {repo}. NÃO implemente nem refine nada — apenas JULGUE, conforme a sua persona, se o escopo está claro o suficiente para avançar.

1. Leia a issue (abaixo) e o template oficial do tipo:
   {fetch_template_cmd}
   (Para feature/bug/refactor, consulte a arquitetura real — docs/system_design/ e o código; clone com `{clone_cmd}` se ainda não houver ./repo. Para bug, verifique se dá pra localizar a origem no código.)
2. Julgue com RIGOR: a issue está CLARA e bem-escopada (segue o template, tem substância para a PRÓXIMA etapa sem ambiguidade) ou VAGO (vazia, template em branco/incompleto, genérica demais, sem alvo/critério)?
3. Seja honesto e específico — se VAGO, aponte exatamente o que falta.

VEREDITO (regra dura): na ÚLTIMA LINHA escreva SOMENTE uma destas, nada depois dela:
  VEREDITO: CLARO
  VEREDITO: VAGO: <o que falta, em uma frase concreta>

=== Issue #{number} (tipo: {type}): {title} ===
{body}
"""

_WORKER_REFINE_BRIEF = """\
Você vai REFINAR a issue #{number} (tipo: {type}) do repositório {repo} — corrigir o TÍTULO e reescrever o CORPO para ficar claro, substancial e dentro do template oficial. NÃO implemente código de feature/fix; o objetivo é deixar o ESCOPO pronto para a próxima etapa.

1. Leia a issue atual (abaixo), o template oficial E OS COMENTÁRIOS da issue:
   {fetch_template_cmd}
   {view_issue_cmd}
   Os comentários — em especial DECISÕES e esclarecimentos do stakeholder — FAZEM PARTE do escopo e DEVEM ser incorporados no corpo. NUNCA ignore o que foi pedido em comentário.
   (feature/refactor: consulte docs/system_design/ e o código real, clone com `{clone_cmd}` se preciso. bug: investigue o código e localize a origem provável arquivo:linha. NUNCA invente.)
2. CORRIJA O TÍTULO para seguir o padrão do template: ele DEVE começar com o prefixo `{title_prefix}`. Aplique:
   {edit_issue_title_cmd}
3. REESCREVA o corpo conforme a estrutura do template, preenchendo CADA seção com substância REAL (do título, do contexto, do código E das decisões registradas nos comentários). Aplique:
   {edit_issue_body_cmd}
4. LACUNA DO STAKEHOLDER: se houver decisão de escopo/lacuna IMPORTANTE que você NÃO pode decidir sozinho com segurança (alto impacto, ou que derivaria uma feature grande adicional), NÃO decida. Em vez disso:
   a) Comente na issue ({comment_issue_cmd}) descrevendo a lacuna E **2 a 3 sugestões bem pensadas** para o stakeholder escolher.
   b) Atribua ao autor (stakeholder): descubra com `{view_pr_author_cmd_for_issue}` e atribua via REST: {assign_user_cmd}
   c) Reporte AGUARDA_STAKEHOLDER (veredito abaixo) — o pipeline pausa o refino até o stakeholder decidir.
5. Honestidade: marque suposições como suposições no corpo; nunca invente fato, dado ou causa-raiz.

VEREDITO (regra dura): na ÚLTIMA LINHA escreva SOMENTE uma destas, nada depois dela:
  REFINO: OK
  REFINO: AGUARDA_STAKEHOLDER

=== Issue #{number} (tipo: {type}): {title} ===
{body}
"""

_WORKER_DECOMPOSE_BRIEF = """\
Você é o ARQUITETO. A intent #{number} do repositório {repo} está CLARA e aprovada. DECOMPONHA-A em uma ou mais issues derivadas INDEPENDENTES (feature/bug/refactor) que possam ser implementadas em branches PARALELOS.

1. Consulte a arquitetura real ANTES de decidir: docs/system_design/ (clone com `{clone_cmd}` se preciso) e o código relevante.
2. Identifique as frentes GENUINAMENTE INDEPENDENTES (sem dependência sequencial entre si). Se a intenção é coesa e indivisível, UMA derivada é a resposta certa; se é multi-frente, várias. NÃO force a divisão — partes acopladas ficam na MESMA issue (fatiar trabalho dependente gera conflito, não paralelismo).
3. Para CADA frente, crie uma issue derivada com escopo JÁ CLARO:
   - título específico; corpo seguindo o template do tipo (.github/ISSUE_TEMPLATE/feature_request.md | bug_report.md | refactor_proposal.md OU .gitlab/issue_templates/<f>.md em projetos GitLab) com alvo técnico, contrato, critérios de aceite e plano de teste;
   - inclua a linha: `Originada de #{number}`.
   - crie com: {create_issue_cmd}
4. Comente na intent #{number} ({comment_issue_cmd}) listando as derivadas criadas com links — ela permanece ABERTA como épico.
5. Honestidade: só liste issues que você REALMENTE criou (confirme com `{view_issue_cmd}`).

VEREDITO (regra dura): na ÚLTIMA LINHA escreva SOMENTE (com os números reais das issues criadas):
  DECOMPOSTO: #<n1> #<n2> ...

=== Intent #{number}: {title} ===
{body}
"""


def _refine_body(body: str) -> str:
    return (body or "").strip()[:ISSUE_BODY_MAX_CHARS] or "(sem corpo — avalie a partir do título)"


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
    cfg = forge or _default_forge_config(repo)
    cmds = render_brief_cmds(
        cfg, number=number, branch=f"refine-{number}", main="main",
        issue_template=template,
    )
    return _WORKER_CRITIQUE_BRIEF.format(
        repo=repo,
        number=number,
        title=title,
        body=_refine_body(body),
        type=issue_type,
        fetch_template_cmd=cmds["fetch_template_cmd"],
        clone_cmd=cmds["clone_cmd"],
    )


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
    cfg = forge or _default_forge_config(repo)
    cmds = render_brief_cmds(
        cfg, number=number, branch=f"refine-{number}", main="main",
        issue_template=template,
    )
    # The "view PR author" command in the renderer assumes a PR; for an
    # issue we adapt it to the proper per-forge issue-author lookup.
    if cfg.kind is ForgeKind.GITHUB:
        view_author_cmd_for_issue = (
            f"gh issue view {number} --repo {repo} --json author -q .author.login"
        )
    else:
        view_author_cmd_for_issue = (
            f"glab api projects/{cfg.project_id or cfg.encoded_project_path}"
            f"/issues/{number} | jq -r .author.username"
        )
    return _WORKER_REFINE_BRIEF.format(
        repo=repo,
        number=number,
        title=title,
        body=_refine_body(body),
        type=issue_type,
        fetch_template_cmd=cmds["fetch_template_cmd"],
        view_issue_cmd=cmds["view_issue_cmd"],
        clone_cmd=cmds["clone_cmd"],
        title_prefix=title_prefix_for_type(issue_type) or "[FEATURE]",
        edit_issue_title_cmd=cmds["edit_issue_title_cmd"],
        edit_issue_body_cmd=cmds["edit_issue_body_cmd"],
        comment_issue_cmd=cmds["comment_issue_cmd"],
        view_pr_author_cmd_for_issue=view_author_cmd_for_issue,
        assign_user_cmd=cmds["assign_user_cmd"],
    )


def _render_worker_decompose_brief(
    repo: str,
    number: int,
    title: str,
    body: str,
    *,
    forge: Optional[ForgeConfig] = None,
) -> str:
    cfg = forge or _default_forge_config(repo)
    cmds = render_brief_cmds(
        cfg, number=number, branch=f"decompose-{number}", main="main",
    )
    return _WORKER_DECOMPOSE_BRIEF.format(
        repo=repo,
        number=number,
        title=title,
        body=_refine_body(body),
        clone_cmd=cmds["clone_cmd"],
        view_issue_cmd=cmds["view_issue_cmd"],
        create_issue_cmd=cmds["create_issue_cmd"],
        comment_issue_cmd=cmds["comment_issue_cmd"],
    )


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
