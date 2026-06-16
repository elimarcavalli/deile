"""Focused unit tests for the pipeline brief renderers (briefs.py).

Each public renderer is exercised directly to lock in placeholder substitution
and the rich-vs-prose divergence of the two mention renderers, hardening the
module against future drift after the SRP extraction out of implementer.py.
"""

from __future__ import annotations

from deile.orchestration.pipeline.briefs import (
    _classify_mention_action,
    _is_spike,
    _render_claude_mention_prompt,
    _render_trigger_details,
    _render_worker_implement_brief,
    _render_worker_implement_resume_brief,
    _render_worker_mention_brief,
    _render_worker_pr_address_brief,
    _render_worker_pr_unified_brief,
    _summarize_trigger_types,
)
from deile.orchestration.pipeline.constants import ISSUE_BODY_MAX_CHARS
from deile.orchestration.pipeline.github_client import (
    CommentRef,
    IssueRef,
    MentionTrigger,
    PrRef,
)


def _issue(number: int = 7) -> IssueRef:
    return IssueRef(
        number=number,
        title=f"Issue {number}",
        url=f"https://github.com/o/r/issues/{number}",
        labels=(),
    )


def _pr(number: int = 9) -> PrRef:
    return PrRef(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/o/r/pull/{number}",
        labels=(),
    )


def _comment(number: int = 5, kind: str = "issue") -> CommentRef:
    return CommentRef(
        comment_id=1,
        body="please @deile-one take a look",
        html_url=f"https://github.com/o/r/issues/{number}#issuecomment-1",
        issue_url=f"https://github.com/o/r/issues/{number}",
        author="alice",
        kind=kind,
    )


class TestWorkerImplementBrief:
    def test_substitutes_all_placeholders(self):
        out = _render_worker_implement_brief(
            "o/r", "main", "feat/x", 42, "Add widget", "do the thing"
        )
        assert "#42" in out
        assert "o/r" in out
        assert "Add widget" in out
        assert "do the thing" in out
        assert "feat/x" in out
        # no unrendered placeholders remain
        assert "{" not in out and "}" not in out

    def test_empty_body_uses_fallback(self):
        out = _render_worker_implement_brief("o/r", "main", "b", 1, "T", "")
        assert "(sem corpo" in out

    def test_long_body_is_truncated(self):
        body = "x" * (ISSUE_BODY_MAX_CHARS + 500)
        out = _render_worker_implement_brief("o/r", "main", "b", 1, "T", body)
        assert "x" * ISSUE_BODY_MAX_CHARS in out
        assert "x" * (ISSUE_BODY_MAX_CHARS + 1) not in out


class TestImplementBriefDefinitionOfDone:
    """Brief de implement carrega o gate de Definição-de-Pronto (causa-raiz da
    PR#605: spike fechou-pela-metade porque "PR existe + testes impactados verdes"
    contava como pronto, e ``Closes`` era automático).
    """

    def test_dod_block_present(self):
        out = _render_worker_implement_brief("o/r", "main", "b", 1, "T", "body")
        assert "DEFINIÇÃO DE PRONTO" in out
        assert "NÃO-VERIFICADO" in out
        # skip de teste de integração não satisfaz AC
        assert "PULA" in out
        assert "@pytest.mark.integration" in out

    def test_resume_brief_also_has_dod_block(self):
        out = _render_worker_implement_resume_brief("o/r", "main", "b", 1, "T", "body")
        assert "DEFINIÇÃO DE PRONTO" in out

    def test_normal_issue_create_pr_uses_closes(self):
        # Asserta no comando de criar PR (o DoD menciona "Refs" como fallback no texto).
        out = _render_worker_implement_brief(
            "o/r", "main", "b", 1, "Add widget", "do it"
        )
        assert '. Closes #1."' in out
        assert '. Refs #1."' not in out

    def test_spike_title_create_pr_uses_refs(self):
        out = _render_worker_implement_brief(
            "o/r", "main", "b", 99, "[SPIKE] Provar X", "faça o spike"
        )
        assert '. Refs #99."' in out
        assert '. Closes #99."' not in out

    def test_spike_exit_condition_body_create_pr_uses_refs(self):
        out = _render_worker_implement_brief(
            "o/r",
            "main",
            "b",
            5,
            "Investigar Y",
            "blah\n## Condição de Saída\nACs verdes com números",
        )
        assert '. Refs #5."' in out
        assert '. Closes #5."' not in out

    def test_spike_resume_brief_create_pr_uses_refs(self):
        # O mesmo invariante de spike vale no brief de resume (briefs.py).
        out = _render_worker_implement_resume_brief(
            "o/r", "main", "b", 13, "[SPIKE] Provar Z", "retoma o spike"
        )
        assert '. Refs #13."' in out
        assert '. Closes #13."' not in out

    def test_normal_resume_brief_create_pr_uses_closes(self):
        out = _render_worker_implement_resume_brief(
            "o/r", "main", "b", 14, "Add widget", "retoma"
        )
        assert '. Closes #14."' in out
        assert '. Refs #14."' not in out

    def test_is_spike_detection(self):
        assert _is_spike("[SPIKE] foo", "")
        assert _is_spike("[spike] foo", "")  # case-insensitive
        assert _is_spike("[ spike ] foo", "")  # whitespace-tolerant
        assert _is_spike("foo", "## Condição de Saída\n...")
        assert _is_spike("foo", "Critérios de Aprovação do Spike: ...")
        assert not _is_spike("[FEATURE] foo", "implementa um botão")
        assert not _is_spike(
            "Add spike-resistant retry", "feature normal"
        )  # 'spike' solto no título não conta
        assert not _is_spike(None, None)  # None-safe

    def test_gitlab_forge_dod_and_spike_refs(self):
        """Cobertura GitLab do gate: o brief renderiza comandos `glab` e o
        spike usa `Refs` no `glab mr create` + o draft cmd no dod_block."""
        from deile.orchestration.forge.base import ForgeConfig, ForgeKind

        gl = ForgeConfig(
            kind=ForgeKind.GITLAB,
            host="gitlab.com",
            project_path="group/project",
            cli_path="/usr/bin/glab",
        )
        # Issue normal (GitLab) → `Closes` + comando glab.
        normal = _render_worker_implement_brief(
            "group/project",
            "main",
            "auto/issue-3",
            3,
            "Add botão",
            "faça",
            forge=gl,
        )
        assert "DEFINIÇÃO DE PRONTO" in normal
        assert "glab mr create" in normal
        assert '. Closes #3."' in normal
        # Spike (GitLab) → `Refs`, nunca `Closes`, e o draft cmd glab no dod_block.
        spike = _render_worker_implement_brief(
            "group/project",
            "main",
            "auto/issue-8",
            8,
            "[SPIKE] Provar Z",
            "spike",
            forge=gl,
        )
        assert '. Refs #8."' in spike
        assert '. Closes #8."' not in spike
        assert (
            "glab mr update auto/issue-8" in spike
        )  # mark_draft_cmd embutido no dod_block


class TestUnifiedPrBrief:
    """Após o refactor "PR é o quadro", os 3 briefs antigos
    (review / review_only / address) foram substituídos por um único
    ``_render_worker_pr_unified_brief`` que descobre o que fazer pelo estado
    real da PR. Os asserts cobrem placeholders renderizados, o ramo "autor
    é HUMANO → NÃO mergeio", o ramo "sou assignee + review APPROVED → MERGEAR"
    e a leitura de ``.deile-progress.md`` no PASSO 0."""

    def test_renders_all_placeholders(self):
        import re

        out = _render_worker_pr_unified_brief(
            "o/r",
            "main",
            11,
            gh_login="deile-one",
        )
        assert "#11" in out and "o/r" in out
        assert "deile-one" in out
        # O brief contém ``{...}`` literais (filtros jq); checamos especificamente
        # se sobrou algum placeholder snake_case não-renderizado.
        unrendered = re.findall(r"\{[a-z_]+\}", out)
        assert unrendered == [], f"placeholders não renderizados: {unrendered}"

    def test_brief_always_executes_request(self):
        out = _render_worker_pr_unified_brief(
            "o/r",
            "main",
            11,
            gh_login="deile-one",
        )
        # Decisão #46: a regra antiga "autor é HUMANO → NUNCA dou push" foi
        # substituída pela regra "execute o pedido SEMPRE", com duas trilhas:
        # push direto se PR open; nova branch + nova PR se PR merged.
        assert "execute o pedido SEMPRE" in out
        assert "está OPEN" in out
        assert "push direto na própria branch" in out
        assert "branch nova derivada" in out
        # Regressão: a antiga cláusula precisa permanecer ausente.
        assert "NUNCA dou push" not in out
        assert "autor é HUMANO" not in out

    def test_brief_states_self_author_merge_path(self):
        out = _render_worker_pr_unified_brief(
            "o/r",
            "main",
            11,
            gh_login="deile-one",
        )
        # Ramo "sou assignee + review APPROVED + threads ok + CI verde → MERGEAR".
        assert "MERGEAR" in out
        assert "APPROVED" in out

    def test_brief_reads_progress_md_at_step_0(self):
        out = _render_worker_pr_unified_brief(
            "o/r",
            "main",
            11,
            gh_login="deile-one",
        )
        assert ".deile-progress.md" in out
        assert "PASSO 0" in out


class TestFullSuiteGate:
    """O brief unificado exige a SUÍTE COMPLETA verde antes de mergear ou
    aprovar — não basta rodar os arquivos do diff (uma mudança quebra
    testes em arquivos que ela não tocou).

    Política de testes por papel:
    - PR unificado (brief único): exige suíte completa antes de mergear/aprovar.
    - IMPLEMENTADOR (implement / implement_resume): usa análise de impacto — roda apenas os
      testes relevantes à mudança. NÃO executa a suíte inteira (tarefa do revisor).
      O brief ainda menciona `pytest deile/tests/` no contexto "NUNCA rode X".
    """

    FULL_SUITE = "pytest deile/tests/"

    def test_unified_pr_brief_requires_full_suite(self):
        out = _render_worker_pr_unified_brief(
            "o/r",
            "main",
            11,
            gh_login="deile-one",
        )
        assert self.FULL_SUITE in out
        assert "SUÍTE COMPLETA" in out

    def test_implement_brief_uses_impact_analysis_not_full_suite(self):
        """Implementador usa análise de impacto — não roda a suíte inteira.

        O brief ainda menciona `pytest deile/tests/` em "NUNCA rode X" (negativo),
        e contém as palavras-chave da estratégia de impacto.
        """
        out = _render_worker_implement_brief("o/r", "main", "b", 1, "T", "body")
        # Menciona full suite apenas no contexto proibitivo ("NUNCA rode").
        assert self.FULL_SUITE in out
        assert "NUNCA rode" in out
        # Contém a estratégia de análise de impacto.
        assert "análise de impacto" in out
        assert "lista_de_testes_impactados" in out
        # NÃO deve exigir rodar a suíte completa como gate positivo.
        assert "SUÍTE COMPLETA" not in out

    def test_implement_resume_brief_uses_impact_analysis_not_full_suite(self):
        """Mesmo que implement, mas para a retomada."""
        out = _render_worker_implement_resume_brief("o/r", "main", "b", 3, "T", "body")
        assert self.FULL_SUITE in out
        assert "NUNCA rode" in out
        assert "análise de impacto" in out
        assert "SUÍTE COMPLETA" not in out


class TestResumeBriefs:
    def test_implement_resume_injects_progress_block(self):
        out = _render_worker_implement_resume_brief("o/r", "main", "b", 3, "T", "body")
        assert "#3" in out
        assert ".deile-progress.md" in out
        assert "{" not in out and "}" not in out


class TestSummarizeTriggerTypes:
    def test_known_labels_are_humanized(self):
        out = _summarize_trigger_types(["assignee", "reviewer"])
        assert "assignee (atribuído a você)" in out
        assert "reviewer (solicitado como revisor)" in out
        assert " + " in out

    def test_unknown_label_passes_through(self):
        assert _summarize_trigger_types(["mystery"]) == "mystery"


class TestClassifyMentionAction:
    def test_reviewer_on_pr(self):
        t = MentionTrigger(trigger_type="reviewer", pr=_pr())
        assert _classify_mention_action(t, ["reviewer"]) == "review_request"

    def test_assignee_on_issue(self):
        t = MentionTrigger(trigger_type="assignee", issue=_issue())
        assert _classify_mention_action(t, ["assignee"]) == "assigned_issue"

    def test_assignee_on_pr(self):
        t = MentionTrigger(trigger_type="assignee", pr=_pr())
        assert _classify_mention_action(t, ["assignee"]) == "assigned_pr"

    def test_comment_on_pr(self):
        t = MentionTrigger(trigger_type="comment", pr=_pr())
        assert _classify_mention_action(t, ["comment"]) == "mention_pr"

    def test_comment_on_issue(self):
        t = MentionTrigger(trigger_type="comment", issue=_issue())
        assert _classify_mention_action(t, ["comment"]) == "mention_issue"

    def test_reviewer_takes_priority_over_assignee(self):
        t = MentionTrigger(trigger_type="reviewer", pr=_pr())
        assert _classify_mention_action(t, ["assignee", "reviewer"]) == "review_request"

    def test_no_match_returns_default(self):
        t = MentionTrigger(trigger_type="comment", comment=_comment(kind="x"))
        # target_kind == "issue" (comment, non-pr_review) but no trigger types match
        assert _classify_mention_action(t, []) == "default"


class TestRenderTriggerDetails:
    def test_rich_emits_markdown(self):
        t = MentionTrigger(trigger_type="assignee", issue=_issue(7))
        out = _render_trigger_details([t], rich=True)
        assert "**Assignado**" in out
        assert "[Issue 7](https://github.com/o/r/issues/7)" in out

    def test_prose_has_no_markdown(self):
        t = MentionTrigger(trigger_type="assignee", issue=_issue(7))
        out = _render_trigger_details([t], rich=False)
        assert "**" not in out
        assert "[" not in out
        assert "Issue 7 (https://github.com/o/r/issues/7)" in out

    def test_comment_body_is_fenced_in_rich(self):
        t = MentionTrigger(trigger_type="comment", comment=_comment())
        rich = _render_trigger_details([t], rich=True)
        prose = _render_trigger_details([t], rich=False)
        assert "```" in rich
        assert "```" not in prose


class TestBriefInvariants:
    """Regression guard: verifica que as substrings críticas do módulo briefs.py
    continuam presentes após qualquer refactor.

    O módulo contém um aviso ⚠ CRITICAL que lista estas strings como invariantes.
    Este teste implementa o item (a) da Decisão #47 / issue #479: falha se qualquer
    substring crítica desaparecer do arquivo.
    """

    # Substrings que devem sempre existir em briefs.py (ver docstring do módulo).
    _CRITICAL_SUBSTRINGS = [
        "CHECKPOINT OBRIGATÓRIO",
        "SUÍTE COMPLETA",
        "anti-eco",
        ".deile-progress.md",
        "DECISÃO É DECISÃO",
        "execute o pedido SEMPRE",
        "REGRA ANTI-FLOOD",
        # FIX #7 (commit 9355289 regressão): regra anti-loop de PR própria e
        # checkout obrigatório no PASSO 0 foram removidos silenciosamente,
        # causando flood de re-review. Estas substrings travam ambas as regras.
        "RESOLVER AGORA, nunca re-revisar",  # anti-loop: PR própria com REQUEST_CHANGES
        "PASSO 0 — Checkout",  # checkout obrigatório antes de qualquer ação
        # Endurecimento do pr_review (homologação multi-CLI): mesmo no merge-direto
        # de PR própria, o reviewer DEVE (a) validar afirmações da PR contra o código
        # e (b) postar uma justificativa substantiva — não só o marcador. Sem
        # reintroduzir loop (justificativa única, no merge/review).
        "VALIDAÇÃO DE AFIRMAÇÕES",  # confronta claim do doc/README vs código real
        "tem que ser SUBSTANTIVO",  # comentário de justificativa obrigatório
    ]

    def _load_briefs_source(self) -> str:
        import pathlib

        briefs_path = (
            pathlib.Path(__file__).parent.parent.parent.parent
            / "orchestration"
            / "pipeline"
            / "briefs.py"
        )
        return briefs_path.read_text(encoding="utf-8")

    def test_critical_substrings_present_in_module(self):
        """Falha se qualquer substring crítica sumir de briefs.py.

        Garante que futuras refatorações não removam silenciosamente guardrails
        operacionais (REGRA ANTI-FLOOD, CHECKPOINT OBRIGATÓRIO, etc.).
        """
        source = self._load_briefs_source()
        missing = [s for s in self._CRITICAL_SUBSTRINGS if s not in source]
        assert missing == [], (
            f"briefs.py está faltando substrings críticas: {missing!r}. "
            "Qualquer mudança que remove estes invariantes exige (a) evidência "
            "empírica de custo equivalente, (b) atualização deste teste."
        )

    def test_anti_flood_cap_in_refine_brief(self):
        """REGRA ANTI-FLOOD deve estar no brief de refinamento com o cap explícito."""
        source = self._load_briefs_source()
        # O cap "MÁXIMO UMA" deve aparecer junto com REGRA ANTI-FLOOD no _WORKER_REFINE_BRIEF.
        assert "MÁXIMO UMA sub-issue" in source, (
            "_WORKER_REFINE_BRIEF perdeu o cap explícito de sub-issues por issue-mãe "
            "(REGRA ANTI-FLOOD: MÁXIMO UMA sub-issue agregada)."
        )

    def test_module_docstring_warning_present(self):
        """O aviso CRITICAL no docstring do módulo deve permanecer."""
        source = self._load_briefs_source()
        assert "CRITICAL: QUALQUER mudança nestes briefs exige" in source, (
            "O aviso ⚠ CRITICAL no docstring de briefs.py foi removido. "
            "Restaure-o para sinalizar a futuros editores as exigências de teste A/B."
        )


class TestMentionRenderers:
    def test_worker_mention_brief_is_markdown(self):
        t = MentionTrigger(trigger_type="reviewer", pr=_pr(9))
        out = _render_worker_mention_brief("o/r", t, ["reviewer"], [t])
        assert "o/r" in out
        assert "REVIEW REQUEST" in out
        assert "#9" in out
        assert "{" not in out and "}" not in out

    def test_claude_mention_prompt_is_prose(self):
        t = MentionTrigger(trigger_type="assignee", issue=_issue(7))
        out = _render_claude_mention_prompt("o/r", t, ["assignee"], [t])
        assert "ASSIGNED" in out
        assert "#7" in out
        # prose renderer must not emit markdown bold
        assert "**" not in out

    def test_both_renderers_pick_same_action_key(self):
        t = MentionTrigger(trigger_type="assignee", pr=_pr(9))
        worker = _render_worker_mention_brief("o/r", t, ["assignee"], [t])
        claude = _render_claude_mention_prompt("o/r", t, ["assignee"], [t])
        assert "ASSIGNED TO PR" in worker
        assert "ASSIGNED TO PR" in claude


class TestAddressReviewBrief:
    """Fix #8 (issue #521) — brief de address-review-feedback.

    Quando a review da PRÓPRIA PR conclui REQUEST_CHANGES com HEAD inalterado,
    o pipeline despacha UMA task de address (implement + push). O brief deve:
    - Renderizar todos os placeholders (sem ``{snake_case}`` sobrando).
    - Instruir o worker a APLICAR o fix (não revisar, comentar veredito, nem mergear).
    - Exigir o push (PUSH é OBRIGATÓRIO — sem push o HEAD não muda).
    - Usar análise de impacto (não rodar a suíte completa — tarefa do revisor).
    - Terminar com SHA do novo HEAD OU BLOQUEADO (não URL de PR).
    """

    FULL_SUITE = "pytest deile/tests/"

    def _render(self):
        return _render_worker_pr_address_brief("o/r", "main", "auto/issue-42", 42)

    def test_renders_all_placeholders(self):
        import re

        out = self._render()
        assert "#42" in out and "o/r" in out
        # Nenhum placeholder snake_case deve sobrar não-renderizado.
        unrendered = re.findall(r"\{[a-z_]+\}", out)
        assert unrendered == [], f"placeholders não renderizados: {unrendered}"

    def test_instrui_aplicar_nao_revisar_nem_mergear(self):
        out = self._render()
        # Instrução central: APLIQUE o fix.
        assert "APLIQUE" in out
        # Proibições explícitas: não revisar, não comentar veredito, não mergear.
        assert "NÃO revise" in out
        assert "NÃO mergeie" in out

    def test_push_e_obrigatorio(self):
        out = self._render()
        # O push deve ser explicitamente obrigatório — sem push o HEAD não muda.
        assert "PUSH é OBRIGATÓRIO" in out

    def test_le_progress_md(self):
        out = self._render()
        assert ".deile-progress.md" in out

    def test_usa_analise_de_impacto_nao_suite_completa(self):
        """Address-review é um IMPLEMENT — usa análise de impacto, não a suíte inteira."""
        out = self._render()
        assert "análise de impacto" in out
        # Menciona full suite apenas no contexto proibitivo ("NUNCA rode").
        assert self.FULL_SUITE in out
        assert "NUNCA rode" in out
        # NÃO exige suíte completa como gate positivo (essa é a regra do revisor).
        assert "SUÍTE COMPLETA" not in out

    def test_ultima_linha_e_sha_ou_bloqueado(self):
        out = self._render()
        # A última linha deve ser SHA do HEAD após push OU BLOQUEADO.
        assert "git rev-parse HEAD" in out
        assert "BLOQUEADO" in out
