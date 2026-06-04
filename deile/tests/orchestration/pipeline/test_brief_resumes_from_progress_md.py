"""Resume é gratuito no brief unificado (Decisão #45).

O PASSO 0 do brief instrui o worker a ler ``.deile-progress.md`` no
diretório de trabalho — se existir, é a TODO da tentativa anterior. Esse
mesmo brief atende tanto fresh quanto resume: não há mais um brief
separado ``_WORKER_REVIEW_RESUME_BRIEF``.

O PASSO 3 instrui escrever ``.deile-progress.md`` quando a work-list NÃO
esvaziou (estourou tempo/orçamento ou impedimento). Próximo tick reusa.
"""

from __future__ import annotations

from deile.orchestration.pipeline.briefs import _render_worker_pr_unified_brief


def _render() -> str:
    return _render_worker_pr_unified_brief(
        "owner/repo", "main", 7, gh_login="deile-one",
    )


class TestBriefResumesFromProgressMd:
    def test_step_0_instructs_to_read_progress_md(self):
        out = _render()
        assert "PASSO 0" in out
        assert ".deile-progress.md" in out
        # cláusula específica: "LEIA-O — é a sua TODO da tentativa anterior"
        assert "TODO da tentativa anterior" in out

    def test_step_3_instructs_to_write_progress_md_when_incomplete(self):
        out = _render()
        assert "PASSO 3" in out
        # cláusula: gravar/atualizar o journal .deile-progress.md
        assert "grave/atualize `.deile-progress.md`" in out
        # reforço B (issue #445 FU): checkpoint INCREMENTAL — o timeout mata
        # o processo sem aviso (rc=124), então grava-se a cada milestone.
        assert "CHECKPOINT INCREMENTAL" in out
        # NÃO dentro de ./repo, NÃO commite
        assert "NÃO commite" in out

    def test_brief_does_not_reset_hard(self):
        """O brief unificado nunca emite ``git reset --hard origin/<main>`` —
        ele sempre opera sobre o checkout/PVC já existente. ``checkout_pr_cmd``
        pode ser ``gh pr checkout <N>`` (ou ``glab mr checkout <N>``), nunca
        um reset destrutivo."""
        out = _render()
        assert "reset --hard origin/" not in out
