"""Focused unit tests for the pipeline brief renderers (briefs.py).

Each public renderer is exercised directly to lock in placeholder substitution
and the rich-vs-prose divergence of the two mention renderers, hardening the
module against future drift after the SRP extraction out of implementer.py.
"""

from __future__ import annotations

from deile.orchestration.pipeline.briefs import (
    _classify_mention_action, _render_claude_mention_prompt,
    _render_trigger_details, _render_worker_implement_brief,
    _render_worker_implement_resume_brief, _render_worker_mention_brief,
    _render_worker_pr_unified_brief, _summarize_trigger_types)
from deile.orchestration.pipeline.constants import ISSUE_BODY_MAX_CHARS
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef,
                                                        MentionTrigger, PrRef)


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
            "o/r", "main", 11, gh_login="deile-one",
        )
        assert "#11" in out and "o/r" in out
        assert "deile-one" in out
        # O brief contém ``{...}`` literais (filtros jq); checamos especificamente
        # se sobrou algum placeholder snake_case não-renderizado.
        unrendered = re.findall(r"\{[a-z_]+\}", out)
        assert unrendered == [], f"placeholders não renderizados: {unrendered}"

    def test_brief_states_human_author_no_push(self):
        out = _render_worker_pr_unified_brief(
            "o/r", "main", 11, gh_login="deile-one",
        )
        # O ramo "autor é HUMANO" no PASSO 1 da work-list.
        assert "autor é HUMANO" in out
        assert "NUNCA dou push" in out

    def test_brief_states_self_author_merge_path(self):
        out = _render_worker_pr_unified_brief(
            "o/r", "main", 11, gh_login="deile-one",
        )
        # Ramo "sou assignee + review APPROVED + threads ok + CI verde → MERGEAR".
        assert "MERGEAR" in out
        assert "APPROVED" in out

    def test_brief_reads_progress_md_at_step_0(self):
        out = _render_worker_pr_unified_brief(
            "o/r", "main", 11, gh_login="deile-one",
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
            "o/r", "main", 11, gh_login="deile-one",
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
        out = _render_worker_implement_resume_brief(
            "o/r", "main", "b", 3, "T", "body"
        )
        assert self.FULL_SUITE in out
        assert "NUNCA rode" in out
        assert "análise de impacto" in out
        assert "SUÍTE COMPLETA" not in out


class TestResumeBriefs:
    def test_implement_resume_injects_progress_block(self):
        out = _render_worker_implement_resume_brief(
            "o/r", "main", "b", 3, "T", "body"
        )
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
        assert (
            _classify_mention_action(t, ["assignee", "reviewer"]) == "review_request"
        )

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
