"""Asserts de conteúdo sobre o brief unificado de PR (Decisão #45).

Cada cláusula crítica do PASSO 1 (work-list por estado) está presente no
template — uma assertion-por-cláusula impede que o brief se desfigure em
edições futuras. Também cobrimos o anti-eco do PASSO 2 (NÃO se
auto-mencione) e o checkpoint obrigatório de comentário visível.
"""

from __future__ import annotations

from deile.orchestration.pipeline.briefs import _render_worker_pr_unified_brief


def _render(repo: str = "owner/repo", number: int = 11) -> str:
    return _render_worker_pr_unified_brief(
        repo, "main", number, gh_login="deile-one",
    )


class TestBriefShortCircuitsWhenHeadCovered:
    """O brief contém a instrução "comentar curto se HEAD igual" — ramo de
    no-op que substitui o storm de revisões redundantes do design antigo."""

    def test_brief_mentions_head_equal_short_comment(self):
        out = _render()
        # cláusula do PASSO 1: "sou reviewer só, review feita, HEAD igual → comentar curto"
        assert "HEAD igual" in out
        # frase específica do template ("já APPROVED em <sha>, sem novidade")
        assert "sem novidade" in out


class TestBriefDoesNotPushWhenPrAuthorIsHuman:
    """O ramo "autor é HUMANO" do PASSO 1 protege contra DEILE dar push em
    PR alheia. Mesmo se o trigger acordou ele, o brief o trava."""

    def test_brief_states_human_author_no_push(self):
        out = _render()
        assert "autor é HUMANO" in out
        assert "NUNCA dou push" in out


class TestBriefExecutesFullWorkListWhenAuthorIsSelf:
    """Os ramos válidos quando ``author == gh_login``:
    - HEAD novo + sou reviewer/assignee → revisar
    - thread aberta dirigida a mim → responder/resolver
    - comment dirigido a mim sem resposta → atender o pedido específico
    - sou assignee + review APPROVED + threads ok + CI verde → MERGEAR"""

    def test_brief_has_revisar_branch(self):
        out = _render()
        assert "revisar" in out.lower() or "REVISAR" in out

    def test_brief_has_thread_open_branch(self):
        out = _render()
        assert "thread aberta" in out.lower() or "thread/notas" in out.lower() or "threads" in out.lower()

    def test_brief_has_comment_directed_to_me_branch(self):
        out = _render()
        # cobertura do ramo "comment dirigido a mim sem resposta → atender o pedido específico"
        assert "comment dirigido a mim sem resposta" in out

    def test_brief_has_merge_branch_with_all_preconditions(self):
        out = _render()
        # "sou assignee + meu_review_atual.state == APPROVED + threads ok + CI verde → MERGEAR"
        assert "MERGEAR" in out
        assert "APPROVED" in out
        assert "threads ok" in out
        assert "CI verde" in out


class TestNoSelfHandleInPostedComments:
    """Anti-eco no PASSO 2: o brief proibe explicitamente que o worker
    auto-mencione seu próprio handle em comments/reviews. A identidade do
    agente é deduzida do ``.user.login``, não do texto."""

    def test_brief_forbids_self_mention(self):
        out = _render()
        assert "NÃO se auto-mencione" in out
        assert "anti-eco" in out


class TestCheckpointObligatoryComment:
    """O checkpoint do brief: a execução é FALHA sem um comment visível."""

    def test_brief_demands_visible_comment(self):
        out = _render()
        assert "CHECKPOINT OBRIGATÓRIO" in out
        # exige a execução do command de comment do forge
        assert "comentário visível" in out
