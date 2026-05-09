"""Tests for PR triage: _classify_new_prs() in PipelineMonitor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import GhCommandError, PrRef
from deile.orchestration.pipeline.labels import REVIEW_PENDING
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)


def _pr(number: int, labels: tuple = ()) -> PrRef:
    return PrRef(
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/o/r/pull/{number}",
        labels=labels,
        head_ref=f"feat/pr-{number}",
    )


def _make_monitor(*, unclassified_prs: list | None = None) -> tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=list(unclassified_prs or []))
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.clear_batch_label = AsyncMock()
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "pr_picked_up", "pr_reviewed",
        "issue_auto_classified", "error", "pr_auto_classified", "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    worktrees = MagicMock()
    claude = MagicMock()
    claude.run = AsyncMock(return_value=ClaudeRunResult(
        returncode=0, stdout="", stderr="", duration_seconds=0.1, cmd=("claude", "-p", "x")
    ))

    monitor = PipelineMonitor(cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier)
    return monitor, github, notifier


class TestClassifyNewPrs:
    async def test_no_unclassified_prs_noop(self):
        monitor, github, notifier = _make_monitor(unclassified_prs=[])
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        notifier.pr_auto_classified.assert_not_called()

    async def test_one_unclassified_pr_gets_label(self):
        pr = _pr(42)
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        await monitor._classify_new_prs()
        github.add_labels.assert_called_once_with("pr", 42, [REVIEW_PENDING])
        github.clear_batch_label.assert_called_once_with("pr", 42)
        notifier.pr_auto_classified.assert_called_once_with(42, pr.title, pr.url)
        assert monitor.stats.prs_classified == 1

    async def test_pr_with_tilde_label_skipped(self):
        """PR that already has a pipeline label (starts with ~) must be skipped."""
        pr = _pr(43, labels=("~workflow:nova",))
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        assert monitor.stats.prs_classified == 0

    async def test_claim_returns_none_skips_pr(self):
        """When claim_with_batch returns None (already claimed), PR is skipped."""
        pr = _pr(44)
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        github.claim_with_batch = AsyncMock(return_value=None)
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        assert monitor.stats.prs_classified == 0

    async def test_claim_raises_gh_error_counts_error_and_continues(self):
        """GhCommandError during claim increments error counter and continues loop."""
        pr1 = _pr(45)
        pr2 = _pr(46)

        async def _claim(kind, number, title):
            if number == 45:
                raise GhCommandError(["gh"], 1, "", "network")
            return "abc"

        monitor, github, notifier = _make_monitor(unclassified_prs=[pr1, pr2])
        github.claim_with_batch = AsyncMock(side_effect=_claim)
        await monitor._classify_new_prs()
        assert monitor.stats.errors == 1
        assert monitor.stats.gh_errors == 1
        # pr2 should still be classified
        github.add_labels.assert_called_once_with("pr", 46, [REVIEW_PENDING])

    async def test_add_labels_raises_counts_error_and_notifies(self):
        """GhCommandError in add_labels increments error counter and calls notifier.error."""
        pr = _pr(47)
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        github.add_labels = AsyncMock(side_effect=GhCommandError(["gh"], 1, "", "label error"))
        await monitor._classify_new_prs()
        assert monitor.stats.errors == 1
        assert monitor.stats.gh_errors == 1
        notifier.error.assert_called_once()
        assert monitor.stats.prs_classified == 0

    async def test_list_unclassified_prs_gh_error_notifies(self):
        """GhCommandError in list_unclassified_prs increments counters and calls notifier.error."""
        monitor, github, notifier = _make_monitor()
        github.list_unclassified_prs = AsyncMock(
            side_effect=GhCommandError(["gh"], 1, "", "network error")
        )
        await monitor._classify_new_prs()
        assert monitor.stats.errors == 1
        assert monitor.stats.gh_errors == 1
        notifier.error.assert_called_once()

    async def test_list_unclassified_prs_generic_error_no_crash(self):
        """Generic exception in list_unclassified_prs is caught and increments error counter."""
        monitor, github, notifier = _make_monitor()
        github.list_unclassified_prs = AsyncMock(side_effect=RuntimeError("unexpected"))
        await monitor._classify_new_prs()
        assert monitor.stats.errors == 1
        notifier.error.assert_not_called()

    async def test_pr_triage_disabled_skips_all(self):
        """When enable_pr_triage=False, _classify_new_prs is not called on tick."""
        pr = _pr(50)
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        monitor.config.enable_pr_triage = False
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_mention_handling = False
        await monitor.tick()
        github.list_unclassified_prs.assert_not_called()
