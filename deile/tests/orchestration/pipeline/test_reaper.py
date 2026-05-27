"""Tests para o reaper de claim órfão (issue #309 fase 3.5 — Fase C).

Cobre:
- Reaper libera PR com ~review:em_andamento stuck há > threshold
- Reaper bloqueia após esgotar attempts (>= reaper_max_attempts)
- Reaper preserva PR fresca (idade < threshold)
- Reaper ignora PR sem ownership label (não toca peer's work)
- Reaper trata forge erros sem derrubar tick
- Reaper desliga quando reaper_stale_seconds=0
- label_applied_at retorna None quando label nunca aplicada
- Attempt label helpers (parse, current_attempt_from_labels, make)
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.orchestration.pipeline.labels import (current_attempt_from_labels,
                                                 is_attempt_label,
                                                 make_attempt_label,
                                                 parse_attempt_label)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.stages import reap_orphan_claims


# ---------------------------------------------------------------------------
# Attempt label helpers
# ---------------------------------------------------------------------------

class TestAttemptLabel:
    def test_make_attempt_label(self):
        assert make_attempt_label(1) == "~attempt:1"
        assert make_attempt_label(99) == "~attempt:99"

    def test_is_attempt_label(self):
        assert is_attempt_label("~attempt:1")
        assert is_attempt_label("~attempt:0")
        assert not is_attempt_label("attempt:1")
        assert not is_attempt_label("~attempt:")
        assert not is_attempt_label("~attempt:abc")
        assert not is_attempt_label("~review:em_andamento")

    def test_parse_attempt_label(self):
        assert parse_attempt_label("~attempt:5") == 5
        with pytest.raises(ValueError):
            parse_attempt_label("~review:em_andamento")

    def test_current_attempt_from_labels(self):
        assert current_attempt_from_labels([]) == 0
        assert current_attempt_from_labels(["~review:em_andamento"]) == 0
        assert current_attempt_from_labels(["~attempt:2", "~review:em_andamento"]) == 2
        # Múltiplos attempt labels — pega o maior.
        assert current_attempt_from_labels(["~attempt:1", "~attempt:3", "~attempt:2"]) == 3


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------

def _make_pr(number, *, labels, head_ref="auto/issue-1"):
    pr = MagicMock()
    pr.number = number
    pr.labels = list(labels)
    pr.head_ref = head_ref
    pr.is_draft = False
    pr.url = f"https://github.com/o/r/pull/{number}"
    pr.title = f"PR #{number}"
    pr.batch_id = next(
        (lb[len("~batch:"):] for lb in labels if lb.startswith("~batch:")), None,
    )
    return pr


def _make_issue(number, *, labels):
    issue = MagicMock()
    issue.number = number
    issue.labels = list(labels)
    issue.title = f"Issue #{number}"
    issue.url = f"https://github.com/o/r/issues/{number}"
    issue.body = ""
    return issue


def _make_monitor_for_reaper(
    *,
    reaper_stale_seconds=2700,
    reaper_max_attempts=3,
):
    cfg = PipelineConfig(
        repo="owner/repo",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        use_pid_lock=False,
        reaper_stale_seconds=reaper_stale_seconds,
        reaper_max_attempts=reaper_max_attempts,
    )
    github = MagicMock()
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.label_applied_at = AsyncMock(return_value=None)
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.comment_on_issue = AsyncMock()
    notifier = MagicMock()
    worktrees = MagicMock()
    claude = MagicMock()
    monitor = PipelineMonitor(
        cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier,
    )
    return monitor, github


@pytest.mark.asyncio
async def test_reaper_releases_stuck_pr_review():
    """PR com ~review:em_andamento + ownership label há > threshold é
    liberada: remove em_andamento+batch+ownership+attempt-antigo,
    adiciona ~review:pendente + ~attempt:(N+1)."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    pr = _make_pr(100, labels=[
        "~review:em_andamento", "~batch:abc12345", own,
    ])
    github.list_open_prs = AsyncMock(return_value=[pr])
    # Aplicada há 120s (acima do threshold de 60s).
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 120)

    await reap_orphan_claims(monitor)

    # Removeu em_andamento, batch e ownership.
    remove_calls = github.remove_labels.await_args_list
    assert len(remove_calls) == 1
    removed = list(remove_calls[0].args[2])  # 3rd posicional = labels iterable
    assert "~review:em_andamento" in removed
    assert "~batch:abc12345" in removed
    assert own in removed
    # Adicionou pendente + attempt:1.
    add_calls = github.add_labels.await_args_list
    assert len(add_calls) == 1
    added = list(add_calls[0].args[2])
    assert "~review:pendente" in added
    assert "~attempt:1" in added


@pytest.mark.asyncio
async def test_reaper_blocks_after_max_attempts():
    """Quando next_attempt >= reaper_max_attempts (3): marca bloqueada +
    attempt:N + comment explicativo. Não recoloca review:pendente."""
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60, reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    # Já no attempt 2 — próximo (3) atinge o cap.
    pr = _make_pr(200, labels=[
        "~review:em_andamento", "~batch:def", own, "~attempt:2",
    ])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    # Adicionou ~workflow:bloqueada + attempt:3 (não pendente).
    added = list(github.add_labels.await_args_list[0].args[2])
    assert "~workflow:bloqueada" in added
    assert "~attempt:3" in added
    assert "~review:pendente" not in added
    # Postou comment.
    github.comment_on_pr.assert_awaited_once()
    msg = github.comment_on_pr.await_args.args[1]
    assert "Reaper esgotou retries" in msg


@pytest.mark.asyncio
async def test_reaper_skips_fresh_pr():
    """PR com idade < threshold não é tocada."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=2700)
    own = monitor.identity.ownership_label()
    pr = _make_pr(300, labels=["~review:em_andamento", own])
    github.list_open_prs = AsyncMock(return_value=[pr])
    # Aplicada há 60s (bem abaixo do threshold de 2700s).
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 60)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_skips_pr_without_ownership():
    """PR de OUTRO monitor (sem ownership label deste monitor) não é tocada."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    # PR sem ownership label deste monitor (own_label não está nas labels).
    pr = _make_pr(400, labels=["~review:em_andamento", "~by:peer-monitor"])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_skips_when_label_applied_at_unknown():
    """Se forge retorna None (sem suporte ou sem evento), pula
    silenciosamente — não toca a PR pq não sabe a idade."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    pr = _make_pr(500, labels=["~review:em_andamento", own])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=None)  # forge não sabe

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_releases_stuck_implement_issue():
    """Issue com ~workflow:em_implementacao stuck → libera pra ~workflow:revisada."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_IMPLEMENTING,
                                                     WORKFLOW_REVIEWED)
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    issue = _make_issue(600, labels=[WORKFLOW_IMPLEMENTING, own])
    github.list_issues_with_label = AsyncMock(return_value=[issue])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    # Issue foi processada.
    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_REVIEWED in added
    assert "~attempt:1" in added


@pytest.mark.asyncio
async def test_reaper_zero_threshold_disabled():
    """Quando ``reaper_stale_seconds=0``, reaper é no-op."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=0)
    own = monitor.identity.ownership_label()
    pr = _make_pr(700, labels=["~review:em_andamento", own])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)

    await reap_orphan_claims(monitor)

    # NÃO chamou list_open_prs nem add/remove.
    github.list_open_prs.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_tolerates_forge_errors():
    """Falhas em add/remove labels durante reap NÃO derrubam o tick
    (são logged + best-effort)."""
    from deile.orchestration.forge import GhCommandError
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    pr = _make_pr(800, labels=["~review:em_andamento", own])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)
    github.remove_labels = AsyncMock(
        side_effect=GhCommandError(("gh", "x"), 1, "", "boom"),
    )

    # Não levanta — log warning e segue.
    await reap_orphan_claims(monitor)


@pytest.mark.asyncio
async def test_reaper_called_in_tick():
    """Verifica que tick() chama reap_orphan_claims quando habilitado."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    # Nenhum PR/issue para reapear, mas as listas devem ser consultadas.
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_issues_with_label = AsyncMock(return_value=[])
    # Skip outros stages (mock).
    monitor.config.enable_classify = False
    monitor.config.enable_review = False
    monitor.config.enable_implement = False
    monitor.config.enable_pr_review = False
    monitor.config.enable_pr_triage = False
    monitor.config.enable_mention_handling = False
    monitor.config.enable_resume = False
    monitor.config.enable_refinement_gate = False

    await monitor.tick()

    # Reaper consultou list_open_prs.
    github.list_open_prs.assert_awaited()
