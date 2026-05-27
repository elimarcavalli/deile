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

_BRIEF_CONSTANTS: dict[str, Any] = {
    "full_suite_cmd": _FULL_SUITE_CMD,
    "pip_guard": _PIP_GUARD,
    "blocked_contract": _BLOCKED_CONTRACT,
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
4. Rode os testes e garanta 100% de aprovação. {pip_guard}. Para iterar rápido durante o desenvolvimento: python3 -m pytest <arquivos_de_teste_novos> -p no:cov -q. MAS, ANTES DE ABRIR A {pr_noun}, rode a SUÍTE COMPLETA — {full_suite_cmd} — e garanta que está 100% verde. Se a sua mudança quebrou um teste FORA dos seus arquivos, conserte-o: uma {pr_noun} com a suíte vermelha NÃO está pronta.
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

_WORKER_REVIEW_BRIEF = """\
Você é o QUALITY GATE final da {pr_noun} #{number} do repositório {repo}. Revise com RIGOR, corrija e — só se passar no portão — mergeie. Execute de verdade: testes verdes NÃO bastam.

CHECKPOINT OBRIGATÓRIO (vale pra QUALQUER worker — agente DEILE, Claude Code, ou outro): a execução é considerada FALHA se você terminar sem ter postado pelo menos UM comentário visível na {pr_noun} via {comment_pr_cmd}. Não basta analisar — o operador precisa VER a sua revisão no {forge_name}. Faça o comentário inicial (mesmo curto: "Iniciando review...") logo ANTES dos testes, e o comentário final com evidências DEPOIS do veredito.

1. Garanta um clone atualizado de {repo} em ./repo ({clone_cmd} se não existir; senão git fetch origin). Dentro de ./repo: {checkout_pr_cmd}
2. LEIA O DIFF INTEIRO e entenda a intenção da mudança: git diff {main}...HEAD ; git diff HEAD. Liste os arquivos tocados e leia cada um por completo.
2b. CONFRONTE A ENTREGA CONTRA O QUE FOI PEDIDO (passo OBRIGATÓRIO): descubra a issue que esta {pr_noun} fecha (procure `Closes #N`/`Fixes #N` no corpo: {view_pr_body_cmd}). Leia a issue E TODOS os comentários dela: {view_issue_cmd_template}. Liste, item a item, TUDO o que foi pedido — no corpo E nos comentários (decisões do stakeholder fazem parte do escopo) — e verifique se a {pr_noun} entrega CADA item. Se faltou qualquer requisito, ou o autor declarou "concluído" sem cumprir, isso É IMPEDIMENTO (veja o veredito).
3. AVALIE contra o checklist do revisor e anote cada achado (arquivo:linha + problema):
   - Corretude e IDEMPOTÊNCIA: a lógica re-executa sem efeito duplicado? Algo em loop/por tick/agendado re-dispara a cada execução sem claim/dedup/cursor? (storms de processamento duplicado são a classe de bug nº 1 deste projeto)
   - SOLID / SRP / DRY / KISS: responsabilidade única; sem duplicação real nem abstração prematura.
   - Arquitetura hexagonal: núcleo sem SDK externo; componentes via registry; Tool retorna ToolResult; I/O async.
   - SEGURANÇA: input sanitizado antes de shell/SQL/fs; sem segredo em log; sem injeção (nada de f-string em filtro jq/shell/SQL — use --arg/binding).
   - Error handling: sem bare except; exceções tipadas (DEILEError); CancelledError re-raised; nenhum awaitable sem await.
   - Testes cobrem casos de BORDA e a regressão que a {pr_noun} alega corrigir.
   - Packaging/deploy: arquivo novo importado em runtime está no COPY do Dockerfile E no allowlist do .dockerignore?
4. CORRIJA os achados com commits normais (SEM force-push) e dê push. Adicione os testes que faltarem.
5. GATE DE TESTES. Para iterar nas correções: python3 -m pytest <arquivos> -p no:cov -q ({pip_guard}). MAS o VEREDITO exige a SUÍTE COMPLETA: rode {full_suite_cmd} (a suíte inteira, que inclui o gate de cobertura — é o portão real de CI) e ela DEVE estar 100% verde. Suíte vermelha — MESMO num arquivo que a {pr_noun} não tocou, se a mudança a quebrou — é IMPEDIMENTO: NÃO mergeie. Cole a saída REAL da suíte completa na evidência.
5b. THREADS/NOTAS de review pendentes: liste os comentários de review ({list_pr_comments_cmd}). Para CADA thread/nota, JULGUE criticamente se o que foi pedido está realmente correto — NÃO obedeça cegamente. Se procede, RESOLVA (faça a mudança + responda a thread citando o commit). Se NÃO procede, responda a thread com uma JUSTIFICATIVA concreta de por que não fazer. Não deixe thread pendente sem ação ou justificativa.
6. DOCUMENTE as evidências como comentário na {pr_noun} ({comment_pr_cmd}): o que revisou, os achados, as correções, como tratou cada thread, e a saída REAL dos testes.
7. VEREDITO:
   - A entrega cumpre TUDO o que a issue + comentários pediram (passo 2b) E o checklist passou E a SUÍTE COMPLETA está 100% verde (passo 5) → MERGEIE. Você é o autor da {pr_noun}; NÃO use comandos de auto-aprovação (o {forge_name} recusa auto-aprovação). Mergeie via REST: {merge_cmd} (fallback: {merge_fallback_cmd}). Confirme: {check_merged_cmd} (deve ser true/merged).
   - A entrega NÃO cumpre tudo o que foi pedido (faltou requisito do corpo ou de um comentário; o autor disse "concluído" sem terminar) → IMPEDIMENTO: NÃO mergeie. Testes verdes NÃO suprem requisito faltante. Comente o que falta vs. o pedido e escreva `BLOQUEADO: <o que falta>` — devolve ao autor.
   - Impedimento REAL que você não pode resolver com segurança (decisão de produto pendente, falta credencial/segredo, mudança quebraria contrato sem migração) → NÃO mergeie; comente o motivo na {pr_noun} e escreva numa linha começando com `{blocked_contract}`.
8. Na ÚLTIMA LINHA: a URL da {pr_noun} seguida de MERGED (ex.: {pr_url_pattern} MERGED) se mergeou; OU a linha `{blocked_contract}`. NUNCA escreva MERGED sem ter mergeado de fato; NUNCA invente resultado.
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
5. Rode os testes e garanta 100% de aprovação. {pip_guard}. Para iterar rápido: python3 -m pytest <arquivos_de_teste_novos> -p no:cov -q. MAS, ANTES DE ABRIR/CONFIRMAR A {pr_noun}, rode a SUÍTE COMPLETA — {full_suite_cmd} — 100% verde. Se sua mudança quebrou um teste fora dos seus arquivos, conserte-o: {pr_noun} com a suíte vermelha NÃO está pronta.
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

_WORKER_REVIEW_RESUME_BRIEF = """\
RETOMADA do QUALITY GATE da {pr_noun} #{number} do repositório {repo} — uma tentativa anterior já começou. NÃO descarte o trabalho parcial. Testes verdes NÃO bastam.

1. Use o clone existente em ./repo (NÃO rode `git reset --hard`, NÃO apague untracked). Garanta o checkout da {pr_noun}: {checkout_pr_cmd}
2. RECONSTRUA O CONTEXTO: leia o journal da tentativa anterior e o diff/untracked atuais:
{progress_block}
   git diff {main}...HEAD ; git status --porcelain (leia cada arquivo modificado/untracked listado).
3. AVALIE com RIGOR contra o checklist do revisor (corretude/IDEMPOTÊNCIA — re-dispara a cada tick sem claim/dedup?; SOLID/SRP/DRY/KISS; arquitetura hexagonal; SEGURANÇA — injeção em jq/shell/SQL, segredo em log; error handling tipado; testes de borda + a regressão alegada; packaging — arquivo novo no COPY do Dockerfile e no allowlist do .dockerignore). Anote cada achado (arquivo:linha + problema).
3b. CONFRONTE a entrega contra o PEDIDO: descubra a issue (Closes #N no corpo da {pr_noun}), leia-a com TODOS os comentários ({view_issue_cmd_template}) e verifique item a item se a {pr_noun} entrega tudo (corpo + decisões do stakeholder). Faltou requisito ou "concluído" sem cumprir → IMPEDIMENTO, NÃO mergeie (testes verdes não suprem), `BLOQUEADO: <o que falta>`.
4. CORRIJA os achados com commits normais (SEM force-push) e dê push. Adicione os testes que faltarem.
5. GATE DE TESTES. Iterar nas correções: python3 -m pytest <arquivos> -p no:cov -q ({pip_guard}). MAS o VEREDITO exige a SUÍTE COMPLETA verde: {full_suite_cmd} DEVE estar 100% verde. Suíte vermelha — mesmo fora dos arquivos da {pr_noun}, se a mudança a quebrou — é IMPEDIMENTO: NÃO mergeie.
6. DOCUMENTE as evidências como comentário na {pr_noun} ({comment_pr_cmd}), colando a saída REAL da suíte completa.
7. VEREDITO — só mergeie se o checklist passou E a SUÍTE COMPLETA está 100% verde: {merge_cmd} (fallback: {merge_fallback_cmd}). Confirme: {check_merged_cmd} (deve ser true/merged).
8. ANTES DE PARAR, atualize `.deile-progress.md` no diretório de trabalho (fora de ./repo, sem commitar): o que revisou, achados, correções e o que falta.
9. Se um impedimento real impedir o merge com qualidade, escreva `{blocked_contract}`. Caso contrário, na ÚLTIMA LINHA escreva a URL da {pr_noun} seguida de MERGED. NUNCA escreva MERGED sem ter mergeado de fato; NUNCA invente resultado.
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


def _render_worker_review_brief(
    repo: str, main: str, number: int, *, forge: Optional[ForgeConfig] = None,
) -> str:
    # Review brief does not know the branch — but the placeholders that
    # reference it only appear in the implement template; for review we use
    # ``pr/<n>`` as a deterministic stand-in (matches the legacy template
    # which used the same shape).
    branch_for_render = f"pr/{number}"
    params = _build_brief_params(
        repo=repo, main=main, branch=branch_for_render, number=number, forge=forge,
    )
    _inject_view_issue_template(params, number)
    return _render_brief(_WORKER_REVIEW_BRIEF, params=params)


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


def _render_worker_review_resume_brief(
    repo: str, main: str, number: int, *, forge: Optional[ForgeConfig] = None,
) -> str:
    branch_for_render = f"pr/{number}"
    params = _build_brief_params(
        repo=repo, main=main, branch=branch_for_render, number=number, forge=forge,
        extras={"progress_block": _PROGRESS_BLOCK},
    )
    _inject_view_issue_template(params, number)
    return _render_brief(_WORKER_REVIEW_RESUME_BRIEF, params=params)


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


# --- Reviewer-only brief (issue #253 follow-up) -------------------------------
# When DEILE is requested ONLY as a reviewer (not assignee/owner), the operator
# policy is: REVIEW and hand the PR back to its author — never fix, never merge.
# DEILE submits a review via REST and sets the PR author as assignee (even when
# the author is DEILE itself). GitHub removes a requested reviewer from the
# "requested" set once they submit a review, so this is naturally idempotent.
_WORKER_REVIEW_ONLY_BRIEF = """\
Você foi solicitado APENAS como REVISOR da {pr_noun} #{number} do repositório {repo}. Seu papel é SÓ revisar e DEVOLVER ao autor — NÃO corrija o código, NÃO faça commits, NÃO mergeie. Execute de verdade.

1. Garanta um clone atualizado de {repo} em ./repo ({clone_cmd} se não existir; senão git fetch origin). Dentro de ./repo: {checkout_pr_cmd}.
2. LEIA O DIFF INTEIRO e entenda a intenção: git diff {main}...HEAD. Leia cada arquivo tocado.
3. AVALIE com RIGOR contra o checklist do revisor (corretude/IDEMPOTÊNCIA — re-dispara a cada tick sem claim/dedup?; SOLID/SRP/DRY/KISS; arquitetura hexagonal; SEGURANÇA — injeção em jq/shell/SQL, segredo em log; error handling tipado; testes de borda; packaging — arquivo novo no COPY do Dockerfile e no allowlist). Anote cada achado com arquivo:linha.
3b. CONFRONTE A ENTREGA CONTRA O PEDIDO: descubra a issue (Closes #N no corpo: {view_pr_body_cmd}), leia-a com TODOS os comentários ({view_issue_cmd_template}) e verifique item a item se a {pr_noun} entrega tudo (corpo + decisões do stakeholder). Faltou requisito, ou autor disse "concluído" sem cumprir → é bloqueante.
3c. GATE DE TESTES — rode a SUÍTE COMPLETA: {full_suite_cmd} ({pip_guard}; é o portão real de CI, inclui cobertura). NÃO basta rodar os arquivos do diff — uma mudança quebra testes em arquivos que ela não tocou. Suíte vermelha = bloqueante. Cole a saída REAL no corpo da review.
4. POSTE a review com o VEREDITO explícito: {review_post_cmd}
   Escolha o VEREDITO:
   - **APPROVE** — SÓ se NÃO houver nada bloqueante (checklist limpo, entrega cumpre o pedido do passo 3b, E a SUÍTE COMPLETA do passo 3c está 100% verde). EXCEÇÃO: se VOCÊ for o autor da {pr_noun} ({view_pr_author_cmd} == você), o {forge_name} recusa self-approve — nesse caso use `COMMENT` (o assignee-autor finalizará o merge no próximo tick).
   - **REQUEST_CHANGES** — se houver QUALQUER problema bloqueante: achado do checklist, requisito faltando (3b), OU suíte vermelha (3c). Testes verdes do subset NÃO suprem a suíte completa vermelha.
   Você ainda NÃO mergeia — quem mergeia é o autor/assignee (Decisão #32).
5. DEVOLVA ao autor: descubra o autor (AUTOR=$({view_pr_author_cmd})) e marque-o como ASSIGNEE: {assign_user_cmd}. (Mesmo que o autor seja você — é o sinal de "bola de volta pro autor".)
6. NÃO mergeie, NÃO faça commits de correção. Seu trabalho termina ao postar a review e devolver ao autor.
7. Na ÚLTIMA LINHA escreva a URL da {pr_noun} ({pr_url_pattern}). Se algo REAL impediu a review, escreva `{blocked_contract}`. NUNCA invente um resultado.
"""


# --- Address-PR brief: comment/body mention on a PR (no merge) ----------------
# A @mention in a PR comment or body asks DEILE to DO what was requested on that
# PR. It may fix code, but it must NOT merge (only the assignee finalizes a PR).
# It also resolves any pending review threads with critical judgement.
_WORKER_PR_ADDRESS_BRIEF = """\
Você foi MENCIONADO na {pr_noun} #{number} do repositório {repo} (em comentário ou no corpo). Atenda ao que foi pedido — execute de verdade. NÃO mergeie (o merge é do autor/assignee).

1. Garanta um clone atualizado de {repo} em ./repo; dentro dela: {checkout_pr_cmd}.
2. Leia o contexto do que foi pedido (o comentário/corpo que te mencionou) e o diff atual (git diff {main}...HEAD).
3. THREADS/NOTAS pendentes ({list_pr_comments_cmd}): para CADA uma, JULGUE criticamente se o que foi pedido está realmente correto. Se procede, FAÇA a mudança e responda a thread citando o commit. Se NÃO procede, responda com JUSTIFICATIVA concreta. Não deixe thread sem ação ou justificativa.
4. Se a tarefa envolve código, edite, rode os testes (NÃO rode pip install — deps já instaladas; python3 -m pytest <arquivos> -p no:cov -q), faça commit normal (SEM force-push) e push.
5. COMENTE o resultado na {pr_noun} ({comment_pr_cmd}): o que fez, como tratou cada thread, e a saída real dos testes.
6. NÃO mergeie. Na ÚLTIMA LINHA escreva a URL da {pr_noun}. Se um impedimento real surgir, escreva `{blocked_contract}`. NUNCA invente resultado.
"""


def _render_worker_review_only_brief(
    repo: str, main: str, number: int, *, forge: Optional[ForgeConfig] = None,
) -> str:
    branch_for_render = f"pr/{number}"
    params = _build_brief_params(
        repo=repo, main=main, branch=branch_for_render, number=number, forge=forge,
    )
    _inject_view_issue_template(params, number)
    return _render_brief(_WORKER_REVIEW_ONLY_BRIEF, params=params)


def _render_worker_pr_address_brief(
    repo: str, main: str, number: int, *, forge: Optional[ForgeConfig] = None,
) -> str:
    branch_for_render = f"pr/{number}"
    params = _build_brief_params(
        repo=repo, main=main, branch=branch_for_render, number=number, forge=forge,
    )
    return _render_brief(_WORKER_PR_ADDRESS_BRIEF, params=params)


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
