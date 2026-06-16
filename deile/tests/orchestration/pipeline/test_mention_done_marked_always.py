"""Após o refactor "PR é o quadro", o pipeline aplica ``~mention:processado``
uniformemente em todo sticky-success de PR — não há mais exceção pra
``review_only`` (Decisão #45 supersedes #32 nesse eixo).

A racional anterior — não marcar pra deixar o assignee re-disparar e
finalizar o merge — deixou de fazer sentido: o brief unificado já cobre
reviewer-mode E assignee-mode numa única passada. O marker apenas evita
re-dispatch redundante; mudanças reais de estado (HEAD novo) voltam a
entrar pelo trigger natural.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.implementer import WorkOutcome
from deile.orchestration.pipeline.labels import MENTION_DONE
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor


def _make_monitor(*, assigned_prs=None, review_request_prs=None) -> PipelineMonitor:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=list(assigned_prs or []))
    github.list_prs_with_review_requests = AsyncMock(
        return_value=list(review_request_prs or [])
    )
    github.search_items_mentioning = AsyncMock(return_value=([], []))
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.get_issue = AsyncMock(
        return_value=IssueRef(
            number=1,
            title="t",
            url="https://github.com/o/r/issues/1",
            labels=(),
        )
    )

    notifier = MagicMock()
    for attr in ("mention_processed", "error"):
        setattr(notifier, attr, AsyncMock())

    monitor = PipelineMonitor(
        cfg,
        github=github,
        worktrees=MagicMock(),
        claude=MagicMock(),
        notifier=notifier,
    )
    monitor.implementer = MagicMock()
    monitor.implementer.mention = AsyncMock(
        return_value=WorkOutcome(ok=True, text="done"),
    )
    return monitor


def _pr(number: int) -> PrRef:
    return PrRef(
        number=number,
        title="pr",
        url=f"https://github.com/o/r/pull/{number}",
        labels=(),
        head_ref=f"auto/issue-{number}",
    )


class TestMentionDoneMarkedAlways:
    async def test_pr_reviewer_sticky_success_marks_mention_done(self):
        """Reviewer-only no design antigo NÃO marcava ``MENTION_DONE`` — agora
        marca. O dispatch tem succeed, então o marker é aplicado uniformemente."""
        monitor = _make_monitor(review_request_prs=[_pr(88)])
        await monitor._process_mentions()
        # Após sticky-success, o marker é aplicado uma vez.
        monitor.forge.add_labels.assert_called_once_with("pr", 88, [MENTION_DONE])

    async def test_pr_assignee_sticky_success_marks_mention_done(self):
        """Assignee em PR continua marcando — comportamento antigo preservado."""
        monitor = _make_monitor(assigned_prs=[_pr(77)])
        await monitor._process_mentions()
        monitor.forge.add_labels.assert_called_once_with("pr", 77, [MENTION_DONE])

    async def test_pr_reviewer_failed_dispatch_does_not_mark(self):
        """Se o dispatch FALHOU, o marker NÃO é aplicado — sticky retry no
        próximo tick. (Comportamento antigo preservado nesse eixo.)"""
        monitor = _make_monitor(review_request_prs=[_pr(88)])
        monitor.implementer.mention = AsyncMock(
            return_value=WorkOutcome(ok=False, text="", error="boom"),
        )
        await monitor._process_mentions()
        monitor.forge.add_labels.assert_not_called()
