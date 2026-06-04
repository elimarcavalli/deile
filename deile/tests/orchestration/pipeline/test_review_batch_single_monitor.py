"""FIX #6 — review_one_open_pr com monitor único NÃO deve claim_with_batch.

Decisão #33: monitor único (shard_count==1) não deve claimar ~batch: —
o label durável ~review:em_andamento já é o lock. A crítica (_critique_one_issue)
já aplica este guard; o review esqueceu.

Garante que:
1. shard_count==1 + PR fresh → NÃO chama claim_with_batch; fluxo de review prossegue.
2. shard_count>1 + PR fresh → chama claim_with_batch (comportamento antigo preservado).
3. shard_count==1 + batch None (simulando race) → impossível neste path (não chama).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.github_client import PrRef
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.implementer import WorkerImplementer
from deile.orchestration.pipeline.labels import (REVIEW_PENDING)
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor

_NOTIFIER_METHODS = (
    "issue_picked_up", "issue_reviewed", "implementation_started",
    "implementation_finished", "implementation_parked", "implementation_resumed",
    "implementation_blocked", "pr_picked_up", "pr_reviewed",
    "issue_auto_classified", "follow_ups_processed", "error",
    "pr_auto_classified", "mention_processed",
)


class _OkClient:
    """Cliente que aceita nowait (retorna task_id) e wait (retorna ok)."""

    async def dispatch(self, payload, *, wait, endpoint_url=None):
        if not wait:
            return {"task_id": "t-001", "status": "running"}
        return {"ok": True, "summary": "done"}


def _pr(number: int, labels: tuple = ()) -> PrRef:
    return PrRef(
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/o/r/pull/{number}",
        labels=labels,
        head_ref=f"auto/issue-{number}",
    )


def _make(*, shard_count: int = 1, prs=None, claim_returns="abc123",
          review_human_prs: bool = False):
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        enable_classify=False,
        enable_refinement_gate=False,
        enable_mention_handling=False,
        enable_pr_triage=False,
        enable_review=True,
        enable_resume=False,
    )
    # Para multi-monitor, o branch ownership check falha porque is_default=False.
    # Habilitamos enable_review_human_prs para contornar (o teste foca no batch guard).
    cfg.enable_review_human_prs = review_human_prs
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=list(prs or []))
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.claim_with_batch = AsyncMock(return_value=claim_returns)
    github.clear_batch_label = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.transition_pr = AsyncMock()
    github.branch_exists = AsyncMock(return_value=True)

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    ledger = DispatchLedger(path=Path(tempfile.mkdtemp()) / "dispatches.json")
    client = _OkClient()
    implementer = WorkerImplementer(client=client, ledger=ledger)

    monitor = PipelineMonitor(
        cfg, github=github, notifier=notifier, implementer=implementer,
        worktrees=MagicMock(), claude=MagicMock(),
    )
    monitor.identity = MonitorIdentity(
        monitor_id="default", shard_index=0, shard_count=shard_count,
    )
    return monitor, github


class TestReviewBatchGuardSingleMonitor:
    """shard_count==1 → NÃO deve chamar claim_with_batch."""

    async def test_single_monitor_fresh_pr_does_not_claim_batch(self):
        """monitor único + PR fresh: NÃO chama claim_with_batch.

        O lock durável é ~review:em_andamento. O FIX está em stages.py:
        review_one_open_pr deve guardar claim_with_batch com shard_count > 1.
        """
        pr = _pr(10, labels=(REVIEW_PENDING,))
        monitor, github = _make(shard_count=1, prs=[pr])

        await monitor._review_one_open_pr()

        github.claim_with_batch.assert_not_called(), (
            "Monitor único NÃO deve chamar claim_with_batch — "
            "gera add/remove do label ~batch: a cada tick sem necessidade."
        )

    async def test_single_monitor_review_proceeds_without_batch(self):
        """monitor único + PR fresh: fluxo de review prossegue (transition_pr chama)."""
        pr = _pr(10, labels=(REVIEW_PENDING,))
        monitor, github = _make(shard_count=1, prs=[pr])

        await monitor._review_one_open_pr()

        # Deve ter transicionado pendente → em_andamento.
        github.transition_pr.assert_called()

    async def test_multi_monitor_fresh_pr_claims_batch(self):
        """shard_count>1 + PR fresh: DEVE chamar claim_with_batch (preserva comportamento antigo)."""
        pr = _pr(20, labels=(REVIEW_PENDING,))
        # enable_review_human_prs=True para bypassar o check de branch ownership
        # (que falha com shard_count>1 porque is_default=False usa prefixo diferente).
        # O foco do teste é o guard de batch, não o ownership.
        monitor, github = _make(shard_count=3, prs=[pr], review_human_prs=True)

        await monitor._review_one_open_pr()

        github.claim_with_batch.assert_called_once_with("pr", 20)

    async def test_multi_monitor_claim_none_skips_review(self):
        """shard_count>1 + claim retorna None: PR já foi reclamada por outro monitor → skip."""
        pr = _pr(30, labels=(REVIEW_PENDING,))
        monitor, github = _make(shard_count=2, prs=[pr], claim_returns=None, review_human_prs=True)

        await monitor._review_one_open_pr()

        # Nenhuma transição — PR foi deixada para o outro monitor.
        github.transition_pr.assert_not_called()
