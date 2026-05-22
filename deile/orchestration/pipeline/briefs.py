"""Worker/Claude brief templates and renderers for the autonomous pipeline.

These are the imperative PT-BR prompts the pipeline sends to the executing
agent — the long-running ``deile-worker`` Pod (markdown briefs) or the legacy
``claude -p`` path (plain-prose prompt). They were extracted from
:mod:`deile.orchestration.pipeline.implementer` so that module keeps a single
responsibility (the execution *strategies*) while this one owns the prompt
*text* and its rendering — mirroring how :mod:`claude_dispatcher` already
separates its prompt templates from the dispatch logic.

The briefs are deliberately explicit and imperative: the worker DEILE owns the
full clone → branch → implement → test → PR lifecycle inside its sandbox. The
worker envelope already pins its CWD and forbids escaping it, so the briefs work
strictly under ``./repo`` relative to that workspace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deile.orchestration.pipeline.constants import ISSUE_BODY_MAX_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.github_client import MentionTrigger


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


# Context-aware worker mention brief builder (issue #253)
def _render_worker_mention_brief(
    repo: str,
    ref: "MentionTrigger",
    trigger_types: list[str],
    all_triggers: list["MentionTrigger"],
) -> str:
    """Build a context-rich mention brief from all trigger types."""
    trigger_summary = _summarize_trigger_types(trigger_types)
    trigger_details = _render_trigger_details(all_triggers, rich=True)

    n = ref.target_number
    actions = {
        "review_request": (
            f"**REVIEW REQUEST**: Você foi solicitado como revisor da PR #{n}. "
            f"Faça uma revisão completa (arquitetura, DRY, KISS, SOLID, clean code), "
            f"rode os testes, corrija problemas, faça commit + push, poste evidências como "
            f"comentário na PR e faça o merge."
        ),
        "assigned_issue": (
            f"**ASSIGNED**: Você foi atribuído à issue #{n}. "
            f"Implemente a feature completa, crie testes, abra uma PR. "
            f"Se já existir uma PR para esta issue, verifique se cobre tudo e "
            f"continue a implementação no branch existente."
        ),
        "assigned_pr": (
            f"**ASSIGNED TO PR**: Você foi atribuído à PR #{n}. "
            f"Revise, corrija, teste, faça commit + push e mergeie se estiver pronto."
        ),
        "mention_pr": (
            f"**MENTION ON PR**: Você foi mencionado na PR #{n}. "
            f"Atenda ao que foi pedido no comentário/corpo, trabalhe no branch da PR, "
            f"teste, faça commit + push e poste a resposta como comentário na PR."
        ),
        "mention_issue": (
            f"**MENTION ON ISSUE**: Você foi mencionado na issue #{n}. "
            f"Atenda ao que foi pedido no comentário/corpo. "
            f"Se a issue já tem PR aberta, trabalhe no branch da PR."
        ),
        "default": "Atenda ao contexto acima da forma mais apropriada.",
    }
    expected_action = actions[_classify_mention_action(ref, trigger_types)]

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


def _render_worker_review_brief(repo: str, main: str, number: int) -> str:
    return _WORKER_REVIEW_BRIEF.format(repo=repo, main=main, number=number)


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
    repo: str, main: str, number: int
) -> str:
    return _WORKER_REVIEW_RESUME_BRIEF.format(
        repo=repo, main=main, number=number, progress_block=_PROGRESS_BLOCK
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
4. POSTE a review via REST com o VEREDITO explícito: gh api -X POST repos/{repo}/pulls/{number}/reviews -f event=<EVENT> -f body="<resumo dos achados, arquivo:linha>". Escolha o EVENT:
   - **APPROVE** — se NÃO houver nada bloqueante (a PR está pronta para merge). EXCEÇÃO: se VOCÊ for o autor da PR (`gh pr view {number} --json author -q .author.login` == você), o GitHub recusa self-approve — nesse caso use `COMMENT` (o assignee-autor finalizará o merge no próximo tick).
   - **REQUEST_CHANGES** — se houver QUALQUER problema bloqueante que precise ser corrigido antes do merge.
   Você ainda NÃO mergeia — quem mergeia é o autor/assignee (Decisão #32).
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
    trigger_summary = _summarize_trigger_types(trigger_types)
    trigger_details = _render_trigger_details(all_triggers, rich=False)

    n = ref.target_number
    actions = {
        "review_request": (
            f"REVIEW REQUEST: Revise a PR #{n} completamente "
            f"(arquitetura, DRY, KISS, SOLID, clean code), rode testes, corrija, "
            f"commit + push, comente evidências e faça merge."
        ),
        "assigned_issue": (
            f"ASSIGNED: Implemente a issue #{n} completa. "
            f"Crie testes, abra PR. Se já existir PR, continue no branch existente."
        ),
        "assigned_pr": (
            f"ASSIGNED TO PR: Revise/corrija a PR #{n}, "
            f"teste, commit + push e mergeie."
        ),
        "mention_pr": (
            f"MENTION ON PR: Atenda ao pedido na PR #{n}, "
            f"trabalhe no branch da PR, teste, commit + push, comente resposta."
        ),
        "mention_issue": (
            f"MENTION ON ISSUE: Atenda ao pedido na issue #{n}. "
            f"Se a issue já tem PR aberta, trabalhe no branch da PR."
        ),
        "default": "Atenda ao contexto acima da forma mais apropriada.",
    }
    action = actions[_classify_mention_action(ref, trigger_types)]

    return (
        f"Você foi acionado por {trigger_summary} no repositório {repo}.\n\n"
        f"Contexto:\n{trigger_details}\n\n"
        f"Ação esperada:\n{action}\n\n"
        f"IMPORTANTE: Poste a resposta como comentário no GitHub usando gh. "
        f"Na última linha, escreva a URL relevante."
    )
