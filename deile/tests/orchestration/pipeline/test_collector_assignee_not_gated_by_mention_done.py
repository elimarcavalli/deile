"""Sticky triggers PR (assignee/reviewer) deixaram de ser gateados por
``~mention:processado`` (Decisão #45 — "PR é o quadro").

A racional anterior — gatear sticky-PR pelo marker pra evitar storm cross-tick
— foi substituída por descoberta-por-estado: o brief unificado abre a PR, vê
o estado real (HEAD vs último review, threads abertas) e comenta curto "sem
novidade" se nada precisa ser feito. O pipeline marca sticky-success com o
marker para evitar churn redundante; mudanças reais (HEAD novo) re-armam via
o trigger natural.

O gate em **body** continua ATIVO — corpo é estático e re-dispararia
infinitamente sem o marker. Esse teste valida ambos os comportamentos.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.labels import MENTION_DONE
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
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
        cfg, github=github, worktrees=MagicMock(), claude=MagicMock(),
        notifier=notifier,
    )


def _issue(number: int, labels=()) -> IssueRef:
    return IssueRef(
        number=number, title="t", url=f"https://github.com/o/r/issues/{number}",
        labels=tuple(labels),
    )


def _pr(number: int, labels=()) -> PrRef:
    return PrRef(
        number=number, title="pr", url=f"https://github.com/o/r/pull/{number}",
        labels=tuple(labels), head_ref=f"auto/issue-{number}",
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

    async def test_reviewer_pr_with_mention_done_still_arms(self):
        """Idem para requested-reviewer em PR."""
        monitor = _make_monitor(review_request_prs=[_pr(88, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "reviewer"

    async def test_assignee_issue_with_mention_done_still_arms(self):
        """Assignee em ISSUE também não é mais gateado pelo marker — a
        injeção em ``~workflow:nova`` (caminho de routing) cuida da
        idempotência do lado do pipeline, não do collector."""
        monitor = _make_monitor(assigned_issues=[_issue(42, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].issue is not None
        assert triggers[0].issue.number == 42


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
