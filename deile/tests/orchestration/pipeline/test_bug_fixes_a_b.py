"""Tests para Bug A + Bug B fixes (issue #309 fase 3.5 — Fase D).

Bug A: erro não-auth do worker em ``stages.review_one_open_pr`` caía no
fast-finish legacy abaixo, marcando ~review:concluida sem proof-of-work.
Bug B: legacy path marcava CONCLUDED sem checar evidência de trabalho.

Fix: erros não-auth liberam batch + retornam (reaper retoma). Antes do
legacy CONCLUDED, ``_assert_review_proof_of_work`` valida atividade do bot.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.orchestration.pipeline.stages import (_assert_review_proof_of_work,
                                                 _resolve_bot_login)


@pytest.mark.asyncio
async def test_proof_of_work_returns_true_when_bot_active(monkeypatch):
    """Bot ativo (forge.has_bot_activity_since=True) → True."""
    forge = MagicMock()
    forge.has_bot_activity_since = AsyncMock(return_value=True)
    result = await _assert_review_proof_of_work(
        forge, "pr", 100, "deile-one", since_ts=1716000000,
    )
    assert result is True
    forge.has_bot_activity_since.assert_awaited_once()


@pytest.mark.asyncio
async def test_proof_of_work_returns_false_when_bot_silent(monkeypatch):
    """Bot SEM atividade → False (impede CONCLUDED sem trabalho)."""
    forge = MagicMock()
    forge.has_bot_activity_since = AsyncMock(return_value=False)
    result = await _assert_review_proof_of_work(
        forge, "pr", 100, "deile-one", since_ts=1716000000,
    )
    assert result is False


@pytest.mark.asyncio
async def test_proof_of_work_fail_open_on_forge_error(monkeypatch):
    """Forge levanta exception → True (fail-open, não bloqueia o pipeline)."""
    forge = MagicMock()
    forge.has_bot_activity_since = AsyncMock(side_effect=RuntimeError("net"))
    result = await _assert_review_proof_of_work(
        forge, "pr", 100, "deile-one", since_ts=1716000000,
    )
    assert result is True


@pytest.mark.asyncio
async def test_proof_of_work_fail_open_when_forge_missing_method(monkeypatch):
    """Forge antigo sem ``has_bot_activity_since`` → True (fail-open)."""
    # MagicMock auto-spec: ainda tem o atributo mesmo se não declaro,
    # mas vou simular forge sem o método.
    class FakeOldForge:
        pass
    forge = FakeOldForge()
    result = await _assert_review_proof_of_work(
        forge, "pr", 100, "deile-one", since_ts=1716000000,
    )
    assert result is True


@pytest.mark.asyncio
async def test_resolve_bot_login_default():
    """V1 hardcoded — sempre 'deile-one'."""
    monitor = MagicMock()
    assert await _resolve_bot_login(monitor) == "deile-one"


@pytest.mark.asyncio
async def test_outcome_error_non_auth_does_not_fast_finish():
    """Bug A regression test: worker erro NÃO-auth + NÃO-merged + NÃO-blocked
    + resume_enabled=False NÃO deve marcar CONCLUDED.

    Em vez disso, deve LIBERAR o batch (reaper retoma no próximo tick).
    """
    # Setup mínimo do monitor pra rodar review_one_open_pr.
    from pathlib import Path

    from deile.orchestration.pipeline.implementer import WorkOutcome
    from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                      PipelineMonitor)

    cfg = PipelineConfig(
        repo="owner/r", base_repo_path=Path("/tmp"), notify_user_id="42",
        use_pid_lock=False, reaper_stale_seconds=0,
        enable_resume=False,  # garante caminho legacy
    )
    github = MagicMock()
    own = "~by:default"
    pr = MagicMock()
    pr.number = 999
    pr.head_ref = "auto/issue-999"
    pr.is_draft = False
    pr.title = "test"
    pr.url = "u"
    pr.labels = ["~review:pendente"]  # fresh PR (não em_andamento)
    pr.batch_id = None

    github.list_open_prs = AsyncMock(return_value=[pr])
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.claim_with_batch = AsyncMock(return_value="batch1")
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.transition_pr = AsyncMock()
    github.clear_batch_label = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.has_bot_activity_since = AsyncMock(return_value=False)

    notifier = MagicMock()
    notifier.pr_picked_up = AsyncMock()
    notifier.pr_reviewed = AsyncMock()
    worktrees = MagicMock()
    claude = MagicMock()

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier,
    )
    # Forge expõe own_label via identity — vamos forçar batch
    own = monitor.identity.ownership_label()
    pr.labels = ["~review:pendente"]

    # Implementer retorna erro NÃO-auth (não merged, não blocked).
    monitor.implementer = MagicMock()
    monitor.implementer.review = AsyncMock(return_value=WorkOutcome(
        ok=False, text="", error="WORKER_TIMEOUT: deadline 100s",
    ))

    from deile.orchestration.pipeline.stages import review_one_open_pr
    await review_one_open_pr(monitor)

    # Verifica: NÃO marcou CONCLUDED.
    transitions = [c for c in github.transition_pr.await_args_list
                   if "concluida" in str(c).lower()]
    assert not transitions, \
        "Bug A regression: pipeline marcou CONCLUDED após erro do worker"
    # Verifica: liberou batch.
    github.clear_batch_label.assert_awaited()


@pytest.mark.asyncio
async def test_proof_of_work_blocks_legacy_finish_without_evidence():
    """Bug B regression: ok=True + not merged + not blocked + sem proof-
    of-work NÃO deve marcar CONCLUDED."""
    from pathlib import Path

    from deile.orchestration.pipeline.implementer import WorkOutcome
    from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                      PipelineMonitor)

    cfg = PipelineConfig(
        repo="owner/r", base_repo_path=Path("/tmp"), notify_user_id="42",
        use_pid_lock=False, reaper_stale_seconds=0, enable_resume=False,
    )
    github = MagicMock()
    pr = MagicMock()
    pr.number = 888
    pr.head_ref = "auto/issue-888"
    pr.is_draft = False
    pr.title = "test"
    pr.url = "u"
    pr.labels = ["~review:pendente"]
    pr.batch_id = None

    github.list_open_prs = AsyncMock(return_value=[pr])
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.claim_with_batch = AsyncMock(return_value="b1")
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.transition_pr = AsyncMock()
    github.clear_batch_label = AsyncMock()
    github.comment_on_pr = AsyncMock()
    # SEM proof-of-work (bot silencioso).
    github.has_bot_activity_since = AsyncMock(return_value=False)

    notifier = MagicMock()
    notifier.pr_picked_up = AsyncMock()
    notifier.pr_reviewed = AsyncMock()
    worktrees = MagicMock()
    claude = MagicMock()

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier,
    )

    # ok=True mas NÃO merged ("merged" não está no texto), NÃO blocked.
    monitor.implementer = MagicMock()
    monitor.implementer.review = AsyncMock(return_value=WorkOutcome(
        ok=True, text="something happened but no actual work",  # ✗ não merged
    ))

    from deile.orchestration.pipeline.stages import review_one_open_pr
    await review_one_open_pr(monitor)

    # Verifica: NÃO marcou CONCLUDED.
    transitions = [
        c for c in github.transition_pr.await_args_list
        if c.kwargs.get("to_label") and "concluida" in c.kwargs["to_label"]
    ]
    assert not transitions, \
        "Bug B regression: pipeline marcou CONCLUDED sem proof-of-work"
    github.clear_batch_label.assert_awaited()
