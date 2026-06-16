"""Gating dos sticky triggers de PR por ``~mention:processado``.

- **assignee (PR/issue)** — NÃO gateado: roteia para ``work_merge`` (Decisão #45).
- **reviewer (PR)** — NÃO gateado pelo marker: a CONCORRÊNCIA é responsabilidade
  do claude-worker (cap global por leases vivas no PVC → 409 quando cheio), NÃO
  do pipeline somando labels. O pipeline dispara para todo review-request; o
  worker recusa o excedente. Um review submetido limpa o ``requested_reviewers``
  no GitHub (some do poll); um review que falhou re-tenta sozinho (sem marker
  que trave). Ver claude_worker_server ``_count_live_leases``.
- **body** — GATEADO: corpo é estático e re-dispararia infinitamente sem o
  marker (não há request que se auto-limpe).

Esse teste valida os três comportamentos.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.labels import MENTION_DONE
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor
from deile.orchestration.pipeline.stages import _collect_mention_triggers


def _make_monitor(
    *,
    assigned_issues: list | None = None,
    assigned_prs: list | None = None,
    review_request_prs: list | None = None,
    body_issues: list | None = None,
    body_prs: list | None = None,
) -> PipelineMonitor:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=list(assigned_issues or []))
    github.list_prs_assigned_to = AsyncMock(return_value=list(assigned_prs or []))
    github.list_prs_with_review_requests = AsyncMock(
        return_value=list(review_request_prs or [])
    )
    github.search_items_mentioning = AsyncMock(
        return_value=(list(body_issues or []), list(body_prs or []))
    )
    notifier = MagicMock()
    notifier.error = AsyncMock()
    return PipelineMonitor(
        cfg,
        github=github,
        worktrees=MagicMock(),
        claude=MagicMock(),
        notifier=notifier,
    )


def _issue(number: int, labels=()) -> IssueRef:
    return IssueRef(
        number=number,
        title="t",
        url=f"https://github.com/o/r/issues/{number}",
        labels=tuple(labels),
    )


def _pr(number: int, labels=()) -> PrRef:
    return PrRef(
        number=number,
        title="pr",
        url=f"https://github.com/o/r/pull/{number}",
        labels=tuple(labels),
        head_ref=f"auto/issue-{number}",
    )


class TestStickyPrTriggersUngated:
    async def test_assignee_pr_with_mention_done_still_arms(self):
        """Assignee em PR com ``~mention:processado`` continua armando o
        trigger. O gate antigo foi removido — quem decide se há trabalho a
        fazer é o brief unificado olhando o estado real da PR."""
        monitor = _make_monitor(assigned_prs=[_pr(77, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "assignee"
        assert triggers[0].pr is not None
        assert triggers[0].pr.number == 77

    async def test_assignee_issue_with_mention_done_still_arms(self):
        """Assignee em ISSUE também não é mais gateado pelo marker — a
        injeção em ``~workflow:nova`` (caminho de routing) cuida da
        idempotência do lado do pipeline, não do collector."""
        monitor = _make_monitor(assigned_issues=[_issue(42, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].issue is not None
        assert triggers[0].issue.number == 42


class TestReviewerTriggerNotGated:
    """``reviewer`` (PR) NÃO é gateado pelo marker nem por soma-de-label.

    A concorrência é do claude-worker (cap global por leases vivas no PVC → 409),
    não do pipeline. O collector dispara para TODO review-request; o worker
    recusa o excedente. Marker presente ou não, com 1 ou N requests, todos
    armam — quem limita é o worker.
    """

    async def test_reviewer_pr_with_mention_done_still_arms(self):
        """Marker presente NÃO bloqueia — review que falhou re-tenta sozinho."""
        monitor = _make_monitor(review_request_prs=[_pr(88, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "reviewer"
        assert triggers[0].pr.number == 88

    async def test_reviewer_pr_without_mention_done_arms(self):
        monitor = _make_monitor(review_request_prs=[_pr(88)])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "reviewer"

    async def test_all_review_requests_arm_no_pipeline_cap(self):
        """N requests → N triggers (o pipeline NÃO capeia; o worker é a autoridade
        de concorrência via 409 por lease-count)."""
        monitor = _make_monitor(review_request_prs=[_pr(1), _pr(2), _pr(3)])
        monitor.config.max_parallel = 2
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        reviewer = [t for t in triggers if t.trigger_type == "reviewer"]
        assert len(reviewer) == 3


class TestBodyTriggerStillGated:
    """Por contraste: ``body`` continua gateado por ``MENTION_DONE``.

    Corpo é estático: sem o marker, o trigger re-dispararia em todo tick. O
    gate foi MANTIDO no collector para esse caso.
    """

    async def test_body_issue_with_mention_done_is_skipped(self):
        monitor = _make_monitor(body_issues=[_issue(55, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers == []

    async def test_body_pr_with_mention_done_is_skipped(self):
        monitor = _make_monitor(body_prs=[_pr(66, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers == []

    async def test_body_without_mention_done_still_arms(self):
        """Body sem o marker continua armando normalmente."""
        monitor = _make_monitor(body_issues=[_issue(55)])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "body"
