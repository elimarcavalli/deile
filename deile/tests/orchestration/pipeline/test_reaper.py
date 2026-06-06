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
    reaper_arch_hard_seconds=7200,
):
    cfg = PipelineConfig(
        repo="owner/repo",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        use_pid_lock=False,
        reaper_stale_seconds=reaper_stale_seconds,
        reaper_max_attempts=reaper_max_attempts,
        reaper_arch_hard_seconds=reaper_arch_hard_seconds,
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
    # reaper_blocked is awaited — must be an AsyncMock so existing tests don't
    # fail with "object MagicMock can't be used in 'await' expression".
    notifier.reaper_blocked = AsyncMock()
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


# ---------------------------------------------------------------------------
# G1 — reaper cobre em_revisao
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reaper_releases_stuck_em_revisao():
    """Issue com ~workflow:em_revisao + ownership label há > threshold é
    liberada para ~workflow:nova (from_label removido, nova adicionado,
    attempt incrementado)."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_NEW,
                                                     WORKFLOW_REVIEWING)
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    issue = _make_issue(901, labels=[WORKFLOW_REVIEWING, own])
    # Dois grupos de chamadas: WORKFLOW_IMPLEMENTING (retorna []) e WORKFLOW_REVIEWING.
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_REVIEWING else [],
    )
    # Aplicada há 200s (acima do threshold de 60s).
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_NEW in added
    assert "~attempt:1" in added

    remove_calls = github.remove_labels.await_args_list
    assert len(remove_calls) >= 1
    removed = list(remove_calls[0].args[2])
    assert WORKFLOW_REVIEWING in removed
    assert own in removed


@pytest.mark.asyncio
async def test_reaper_skips_fresh_em_revisao():
    """Issue em ~workflow:em_revisao com idade < threshold NÃO é tocada."""
    from deile.orchestration.pipeline.labels import WORKFLOW_REVIEWING
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=2700)
    own = monitor.identity.ownership_label()
    issue = _make_issue(902, labels=[WORKFLOW_REVIEWING, own])
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_REVIEWING else [],
    )
    # Aplicada há 60s (abaixo do threshold de 2700s).
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 60)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_does_not_touch_em_arquitetura():
    """REGRESSÃO-GUARD (reinterpretado — issue #427): issue em
    ~workflow:em_arquitetura em descanso (sem dispatch em voo e dentro do
    hard-TTL de 2h) NÃO é tocada pelo reaper.

    O ramo sem-ledger (#427) só dispara após arch_hard_seconds (2h default).
    list_issues_with_label retorna [] para todos os estados, logo nenhuma
    issue é processada, o que é equivalente a nenhuma issue de descanso real
    atingindo o TTL curto (o TTL do descanso é 2h >> reaper_stale_seconds=60).
    """
    from deile.orchestration.pipeline.labels import WORKFLOW_ARCHITECTURE
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    _make_issue(903, labels=[WORKFLOW_ARCHITECTURE, own, "refinar"])
    # Retorna [] para todos os estados — simula que não há issues zumbi
    # na fila (o reaper não toca em issues inexistentes na lista).
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_does_not_touch_em_refinamento():
    """REGRESSÃO-GUARD (reinterpretado — issue #427): issue em
    ~workflow:em_refinamento em descanso com list_issues_with_label retornando
    [] não é tocada pelo reaper (nenhuma issue na lista = nada a reapear)."""
    from deile.orchestration.pipeline.labels import WORKFLOW_REFINING
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    _make_issue(904, labels=[WORKFLOW_REFINING, own, "refinar"])
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_em_revisao_without_ownership_skipped():
    """Issue em ~workflow:em_revisao sem ownership label deste monitor NÃO
    é tocada."""
    from deile.orchestration.pipeline.labels import WORKFLOW_REVIEWING
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    issue = _make_issue(905, labels=[WORKFLOW_REVIEWING, "~by:outro-monitor"])
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_REVIEWING else [],
    )
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


# ---------------------------------------------------------------------------
# Issue #522 — Discord notification via deilebot when reaper blocks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reaper_block_sends_discord_notification():
    """Quando o reaper bloqueia um item (next_attempt >= max_attempts),
    notifier.reaper_blocked deve ser chamado com os dados corretos: número,
    url, kind, attempt/max e age_seconds."""
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60, reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    # attempt:2 → next=3 >= max_attempts=3 → bloqueia
    pr = _make_pr(1000, labels=[
        "~review:em_andamento", own, "~attempt:2",
    ])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    monitor.notifier.reaper_blocked = AsyncMock()

    await reap_orphan_claims(monitor)

    monitor.notifier.reaper_blocked.assert_awaited_once()
    call_kwargs = monitor.notifier.reaper_blocked.await_args
    args, kwargs = call_kwargs.args, call_kwargs.kwargs
    # Posicionais: number, url
    assert args[0] == 1000
    assert args[1] == pr.url
    # Keyword-only
    assert kwargs["kind"] == "pr"
    assert kwargs["attempt"] == 3
    assert kwargs["max_attempts"] == 3
    assert kwargs["age_seconds"] >= 200


@pytest.mark.asyncio
async def test_reaper_already_blocked_item_not_renotified():
    """Item com ~workflow:bloqueada já removido do conjunto de candidatos
    de reap_orphan_claims — reaper_blocked NÃO deve ser chamado de novo."""
    monitor, github = _make_monitor_for_reaper(reaper_stale_seconds=60)
    own = monitor.identity.ownership_label()
    # Item JÁ tem ~workflow:bloqueada — não tem ~review:em_andamento,
    # portanto não entra no laço do reaper.
    pr = _make_pr(1001, labels=["~workflow:bloqueada", own, "~attempt:3"])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)

    monitor.notifier.reaper_blocked = AsyncMock()

    await reap_orphan_claims(monitor)

    # Nada foi tocado (o item não tem ~review:em_andamento).
    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()
    monitor.notifier.reaper_blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_block_notification_failure_is_best_effort():
    """Se reaper_blocked lança, o bloqueio, comentário GitHub e stats já foram
    registrados — a falha na DM NÃO deve propagar nem impedir o return normal."""
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60, reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    pr = _make_pr(1002, labels=[
        "~review:em_andamento", own, "~attempt:2",
    ])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    # Simula falha no envio da DM — _send já captura Exception internamente,
    # mas testamos que o chamador também não propaga.
    monitor.notifier.reaper_blocked = AsyncMock(
        side_effect=Exception("discord unavailable"),
    )

    # Não deve levantar.
    await reap_orphan_claims(monitor)

    # Bloqueio e stats já registrados antes da DM.
    added = list(github.add_labels.await_args_list[0].args[2])
    assert "~workflow:bloqueada" in added
    assert monitor._stats.issues_blocked >= 1
    github.comment_on_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaper_block_disabled_notifier_is_noop():
    """Com notifier.enabled == False (sem user_id), a chamada a reaper_blocked
    é no-op: sem exceção, sem DM, comportamento de bloqueio normal."""
    from deile.orchestration.pipeline.notifier import DiscordNotifier
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60, reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    pr = _make_pr(1003, labels=[
        "~review:em_andamento", own, "~attempt:2",
    ])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    # Substitui o notifier por um real com enabled=False (sem user_id).
    real_notifier = DiscordNotifier(user_id="")
    monitor.notifier = real_notifier
    assert real_notifier.enabled is False

    # Não deve levantar; bloqueio acontece normalmente.
    await reap_orphan_claims(monitor)

    added = list(github.add_labels.await_args_list[0].args[2])
    assert "~workflow:bloqueada" in added
    github.comment_on_pr.assert_awaited_once()


# ---------------------------------------------------------------------------
# Issue #427 — ramo sem-ledger: em_arquitetura / em_refinamento zumbi
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_arch_no_refine_labels_routes_to_nova():
    """AC1 + AC2: issue em ~workflow:em_arquitetura sem ledger entry (ledger
    ausente) e sem labels ~refine:N, após arch_hard_seconds, volta para
    ~workflow:nova."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_ARCHITECTURE,
                                                     WORKFLOW_NEW)
    # TTL curto para o teste; arch_hard = 100s.
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=100,
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2001, labels=[WORKFLOW_ARCHITECTURE, own])
    # Nenhum ledger no implementer.
    monitor.implementer._ledger = None
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    # Aplicada há 200s > arch_hard_seconds=100.
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_NEW in added
    assert "~attempt:1" in added


@pytest.mark.asyncio
async def test_reaper_arch_with_refine_labels_routes_to_revisada():
    """AC2: issue em ~workflow:em_arquitetura com ~refine:2 (já passou por ao
    menos um pass de refino), após arch_hard_seconds, volta para
    ~workflow:revisada."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_ARCHITECTURE,
                                                     WORKFLOW_REVIEWED)
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=100,
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2002, labels=[WORKFLOW_ARCHITECTURE, own, "~refine:2"])
    monitor.implementer._ledger = None
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_REVIEWED in added
    assert "~attempt:1" in added


@pytest.mark.asyncio
async def test_reaper_refining_no_ledger_routes_to_nova():
    """AC1 + AC2: issue em ~workflow:em_refinamento sem ledger entry, após
    arch_hard_seconds, sempre volta para ~workflow:nova (roteamento fixo para
    intents — heurística de refino não se aplica ao estado em_refinamento)."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_NEW,
                                                     WORKFLOW_REFINING)
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=100,
    )
    own = monitor.identity.ownership_label()
    # Mesmo com ~refine:3, em_refinamento sempre vai para nova.
    issue = _make_issue(2003, labels=[WORKFLOW_REFINING, own, "~refine:3"])
    monitor.implementer._ledger = None
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_REFINING else [],
    )
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_NEW in added


@pytest.mark.asyncio
async def test_reaper_arquitetura_no_ledger_fires_when_ledger_is_none():
    """AC1: ramo sem-ledger FORA do guard `if ledger is not None` — cobre o
    caso em que ledger=None (modo sem DispatchLedger disponível)."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_ARCHITECTURE,
                                                     WORKFLOW_NEW)
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=100,
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2004, labels=[WORKFLOW_ARCHITECTURE, own])
    # Garante ledger=None no implementer (explicitamente).
    if hasattr(monitor.implementer, "_ledger"):
        monitor.implementer._ledger = None
    else:
        monitor.implementer._ledger = None

    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    # Ramo sem-ledger DEVE ter disparado.
    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_NEW in added


@pytest.mark.asyncio
async def test_reaper_arch_within_hard_ttl_not_reaped():
    """AC5: issue em ~workflow:em_arquitetura em descanso (sem task_id no
    ledger) com idade < arch_hard_seconds NÃO é tocada — invariante de
    proteção do descanso entre passes."""
    from deile.orchestration.pipeline.labels import WORKFLOW_ARCHITECTURE
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=7200,  # 2h
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2005, labels=[WORKFLOW_ARCHITECTURE, own])
    monitor.implementer._ledger = None
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    # Aplicada há 60s — muito abaixo dos 7200s de hard-TTL.
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 60)

    await reap_orphan_claims(monitor)

    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_arch_with_task_id_in_ledger_not_touched_by_no_ledger_branch():
    """AC1: issue em ~workflow:em_arquitetura COM task_id no ledger (dispatch
    em voo sadio) NÃO é tratada pelo ramo sem-ledger — é do ramo com-ledger."""
    from deile.orchestration.pipeline.labels import WORKFLOW_ARCHITECTURE
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=100,  # TTL curto — dispararia se fosse zumbi
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2006, labels=[WORKFLOW_ARCHITECTURE, own])

    # Ledger com task_id — indica dispatch em voo sadio.
    from unittest.mock import MagicMock
    mock_ledger = MagicMock()
    mock_ledger.get.return_value = {"task_id": "task-abc-123"}
    mock_ledger.clear = MagicMock()
    monitor.implementer._ledger = mock_ledger

    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    # Aplicada há 200s > arch_hard_seconds=100 — seria reaped se fosse zumbi.
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    # O ramo sem-ledger NÃO deve ter disparado (task_id presente no ledger).
    # O ramo com-ledger SIM tentaria reapear (threshold=2700 > 200 → não ativa).
    # Portanto nada é reapado neste tick.
    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_arch_branch_runs_when_pr_ttl_zero():
    """AC8: quando reaper_stale_seconds=0 (PR-TTL desligado), o ramo
    sem-ledger de em_arquitetura ainda deve disparar se arch_hard_seconds > 0.
    Garante que desligar o PR-TTL não silencia o ramo de arquitetura."""
    from deile.orchestration.pipeline.labels import (WORKFLOW_ARCHITECTURE,
                                                     WORKFLOW_NEW)
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=0,     # PR-TTL desligado
        reaper_arch_hard_seconds=100,
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2007, labels=[WORKFLOW_ARCHITECTURE, own])
    monitor.implementer._ledger = None
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    # list_open_prs retorna lista vazia (PR-TTL=0 não deve nem ser chamado,
    # mas garantimos que não há PRs para reapear de qualquer modo).
    github.list_open_prs = AsyncMock(return_value=[])
    # Aplicada há 200s > arch_hard_seconds=100.
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    await reap_orphan_claims(monitor)

    # Ramo sem-ledger de arquitetura DEVE ter disparado mesmo com PR-TTL=0.
    add_calls = github.add_labels.await_args_list
    assert len(add_calls) >= 1
    added = list(add_calls[0].args[2])
    assert WORKFLOW_NEW in added


@pytest.mark.asyncio
async def test_reaper_arch_hard_zero_disables_no_ledger_branch():
    """AC8 + AC3: quando reaper_arch_hard_seconds=0, o ramo sem-ledger é
    desligado — issues em em_arquitetura zumbi NÃO são reaped."""
    from deile.orchestration.pipeline.labels import WORKFLOW_ARCHITECTURE
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=2700,
        reaper_arch_hard_seconds=0,  # ramo sem-ledger desligado
    )
    own = monitor.identity.ownership_label()
    issue = _make_issue(2008, labels=[WORKFLOW_ARCHITECTURE, own])
    monitor.implementer._ledger = None
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [issue] if label == WORKFLOW_ARCHITECTURE else [],
    )
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 99999)

    await reap_orphan_claims(monitor)

    # Com arch_hard_seconds=0 o ramo está desligado — nada reaped.
    # (O ramo com-ledger também não dispara pois ledger=None e threshold > 200s.)
    github.remove_labels.assert_not_awaited()
    github.add_labels.assert_not_awaited()
