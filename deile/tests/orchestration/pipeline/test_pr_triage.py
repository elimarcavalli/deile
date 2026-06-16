"""Tests for PR triage: _classify_new_prs() in PipelineMonitor.

Triage only labels PRs the pipeline would actually review (branch ownership —
``auto/issue-*`` for default identity, or any branch when
``enable_review_human_prs``), and only claims a ``~batch:`` lock when more than
one monitor runs (single-monitor adds the label directly, no lock churn).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import GhCommandError, PrRef
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.labels import REVIEW_PENDING
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor


def _pr(number: int, labels: tuple = (), head_ref: str | None = None) -> PrRef:
    return PrRef(
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/o/r/pull/{number}",
        labels=labels,
        # Default to an owned (auto/issue-*) branch so the default-identity
        # monitor triages it; tests pass an explicit head_ref for foreign PRs.
        head_ref=head_ref if head_ref is not None else f"auto/issue-{number}",
    )


def _make_monitor(
    *,
    unclassified_prs: list | None = None,
    shard_count: int = 1,
    review_human_prs: bool = False,
) -> tuple[PipelineMonitor, MagicMock, MagicMock]:
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
        "issue_picked_up",
        "issue_reviewed",
        "implementation_started",
        "implementation_finished",
        "implementation_parked",
        "pr_picked_up",
        "pr_reviewed",
        "issue_auto_classified",
        "error",
        "pr_auto_classified",
        "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    worktrees = MagicMock()
    claude = MagicMock()
    claude.run = AsyncMock(
        return_value=ClaudeRunResult(
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            cmd=("claude", "-p", "x"),
        )
    )

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier
    )
    # Inject a multi-monitor identity when requested (so the ~batch: claim path
    # runs); enable_review_human_prs decouples ownership from branch prefix so
    # the claim tests don't depend on per-monitor branch naming.
    monitor.identity = MonitorIdentity(
        monitor_id="default", shard_index=0, shard_count=shard_count
    )
    monitor.config.enable_review_human_prs = review_human_prs
    return monitor, github, notifier


class TestClassifyNewPrs:
    async def test_no_unclassified_prs_noop(self):
        monitor, github, notifier = _make_monitor(unclassified_prs=[])
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        notifier.pr_auto_classified.assert_not_called()

    async def test_owned_pr_gets_label_single_monitor(self):
        """An owned (auto/issue-*) PR is labelled; single monitor doesn't claim."""
        pr = _pr(42)
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        await monitor._classify_new_prs()
        github.add_labels.assert_called_once_with("pr", 42, [REVIEW_PENDING])
        # Single monitor: NO ~batch: churn.
        github.claim_with_batch.assert_not_called()
        github.clear_batch_label.assert_not_called()
        notifier.pr_auto_classified.assert_called_once_with(42, pr.title, pr.url)
        assert monitor.stats.prs_classified == 1

    async def test_foreign_branch_pr_not_triaged(self):
        """A PR on a non-auto branch (human/foreign) is NOT labelled — the
        pipeline would never review it, so it must not get stuck ~review:pendente."""
        pr = _pr(43, head_ref="feat/human-change")
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        github.claim_with_batch.assert_not_called()
        assert monitor.stats.prs_classified == 0

    async def test_review_human_prs_triages_foreign_branch(self):
        """With enable_review_human_prs, even a foreign branch is triaged."""
        pr = _pr(43, head_ref="feat/human-change")
        monitor, github, notifier = _make_monitor(
            unclassified_prs=[pr], review_human_prs=True
        )
        await monitor._classify_new_prs()
        github.add_labels.assert_called_once_with("pr", 43, [REVIEW_PENDING])
        assert monitor.stats.prs_classified == 1

    async def test_pr_with_tilde_label_skipped(self):
        """PR that already has a pipeline label (starts with ~) must be skipped."""
        pr = _pr(43, labels=("~workflow:nova",))
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr])
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        assert monitor.stats.prs_classified == 0

    async def test_multi_monitor_claims_and_clears(self):
        """With >1 monitor, the ~batch: lock IS claimed and released.

        Uses an ``auto/default/issue-*`` branch (the prefix a 2-shard identity
        owns) so this exercises the REAL ownership path, not the
        ``enable_review_human_prs`` bypass (review note #1 on PR #264)."""
        pr = _pr(42, head_ref="auto/default/issue-42")
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr], shard_count=2)
        await monitor._classify_new_prs()
        github.claim_with_batch.assert_called_once_with("pr", 42)
        github.add_labels.assert_called_once_with("pr", 42, [REVIEW_PENDING])
        github.clear_batch_label.assert_called_once_with("pr", 42)
        assert monitor.stats.prs_classified == 1

    async def test_claim_returns_none_skips_pr(self):
        """When claim_with_batch returns None (already claimed), PR is skipped
        (multi-monitor only — single monitor never claims)."""
        pr = _pr(44, head_ref="auto/default/issue-44")
        monitor, github, notifier = _make_monitor(unclassified_prs=[pr], shard_count=2)
        github.claim_with_batch = AsyncMock(return_value=None)
        await monitor._classify_new_prs()
        github.add_labels.assert_not_called()
        assert monitor.stats.prs_classified == 0

    async def test_claim_raises_gh_error_counts_error_and_continues(self):
        """GhCommandError during claim increments error counter and continues loop."""
        pr1 = _pr(45, head_ref="auto/default/issue-45")
        pr2 = _pr(46, head_ref="auto/default/issue-46")

        async def _claim(kind, number):
            if number == 45:
                raise GhCommandError(["gh"], 1, "", "network")
            return "abc"

        monitor, github, notifier = _make_monitor(
            unclassified_prs=[pr1, pr2], shard_count=2
        )
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
        github.add_labels = AsyncMock(
            side_effect=GhCommandError(["gh"], 1, "", "label error")
        )
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
