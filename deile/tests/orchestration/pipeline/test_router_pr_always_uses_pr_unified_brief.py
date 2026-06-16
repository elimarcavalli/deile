"""``_dispatch_mention_group`` resolve qualquer trigger PR para ``pr_unified``
(Decisão #45 — "PR é o quadro").

A racional dos 3 modes anteriores (``work_merge`` / ``review_only`` /
``address``) era escolher o BRIEF baseado no trigger. Após o refactor, o
brief é único — descobre o que fazer pelo estado real da PR — então não há
mais escolha de mode pelo router. O ramo de issue-comment mantém o mode
``comment`` legacy.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import CommentRef, IssueRef, PrRef
from deile.orchestration.pipeline.implementer import WorkOutcome
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor


def _comment_pr_review(
    comment_id: int = 1, body: str = "@deile-one tweak X"
) -> CommentRef:
    return CommentRef(
        comment_id=comment_id,
        body=body,
        html_url=f"https://github.com/o/r/pull/9#discussion_r{comment_id}",
        issue_url="https://api.github.com/repos/o/r/pull/9",
        author="elimarcavalli",
        kind="pr_review",
    )


def _pr(number: int = 9) -> PrRef:
    return PrRef(
        number=number,
        title="pr",
        url=f"https://github.com/o/r/pull/{number}",
        labels=(),
        head_ref=f"auto/issue-{number}",
    )


def _issue(number: int = 42) -> IssueRef:
    return IssueRef(
        number=number,
        title="t",
        url=f"https://github.com/o/r/issues/{number}",
        labels=(),
    )


def _make_monitor(
    *,
    pr_comments: list | None = None,
    assigned_prs: list | None = None,
    review_request_prs: list | None = None,
) -> PipelineMonitor:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(
        return_value=list(pr_comments or [])
    )
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


class TestRouterPrAlwaysUsesPrUnifiedBrief:
    """Validamos via spy no ``implementer.mention`` que TODO trigger sobre PR
    resolve para ``mode="pr_unified"``, independente do papel acionado."""

    async def test_pr_assignee_resolves_to_pr_unified(self):
        monitor = _make_monitor(assigned_prs=[_pr(7)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_reviewer_resolves_to_pr_unified(self):
        monitor = _make_monitor(review_request_prs=[_pr(8)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_comment_resolves_to_pr_unified(self):
        monitor = _make_monitor(pr_comments=[_comment_pr_review(1)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_assignee_plus_reviewer_resolves_to_pr_unified(self):
        """Quando o mesmo PR aparece em múltiplos triggers, o router ainda
        resolve UMA vez para ``pr_unified`` (dedup pelo target funciona como
        antes — só o mode mudou)."""
        monitor = _make_monitor(
            assigned_prs=[_pr(9)],
            review_request_prs=[_pr(9)],
        )
        await monitor._process_mentions()
        # uma única chamada — dedup por target
        assert monitor.implementer.mention.call_count == 1
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_assignee_plus_pr_comment_resolves_to_pr_unified(self):
        """Mix de sticky+comment no mesmo PR também → 1 dispatch ``pr_unified``."""
        monitor = _make_monitor(
            assigned_prs=[_pr(9)],
            pr_comments=[_comment_pr_review(1, body="@deile-one olha lá")],
        )
        # ajustar o html_url do comment para alinhar com pr#9
        monitor.forge.list_pr_review_comments_since = AsyncMock(
            return_value=[
                CommentRef(
                    comment_id=1,
                    body="@deile-one olha lá",
                    html_url="https://github.com/o/r/pull/9#discussion_r1",
                    issue_url="https://api.github.com/repos/o/r/pull/9",
                    author="elimarcavalli",
                    kind="pr_review",
                )
            ]
        )
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_count == 1
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"
