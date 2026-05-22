"""Tests for mention handling: _process_mentions() in PipelineMonitor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef, PrRef)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)


def _comment(
    comment_id: int,
    body: str,
    *,
    author: str = "user1",
    kind: str = "issue",
) -> CommentRef:
    return CommentRef(
        comment_id=comment_id,
        body=body,
        html_url=f"https://github.com/o/r/issues/1#issuecomment-{comment_id}",
        issue_url="https://api.github.com/repos/o/r/issues/1",
        author=author,
        kind=kind,
    )


def _make_monitor(
    *,
    issue_comments: list | None = None,
    pr_comments: list | None = None,
    claude_ok: bool = True,
) -> tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=list(issue_comments or []))
    github.list_pr_review_comments_since = AsyncMock(return_value=list(pr_comments or []))
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up", "pr_reviewed",
        "issue_auto_classified", "error", "pr_auto_classified", "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    worktrees = MagicMock()
    claude = MagicMock()
    claude.run = AsyncMock(return_value=ClaudeRunResult(
        returncode=0 if claude_ok else 1,
        stdout="done",
        stderr="",
        duration_seconds=0.1,
        cmd=("claude", "-p", "x"),
    ))

    monitor = PipelineMonitor(cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier)
    return monitor, github, notifier


class TestProcessMentions:
    async def test_no_comments_no_dispatch(self):
        monitor, github, notifier = _make_monitor()
        await monitor._process_mentions()
        notifier.mention_processed.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_comment_with_mention_dispatches(self):
        comment = _comment(1, "Hey @deile-one can you help?")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once_with(comment.html_url, comment.author)
        assert monitor.stats.mentions_processed == 1

    async def test_comment_without_mention_skipped(self):
        comment = _comment(2, "Just a regular comment, no mention here")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        notifier.mention_processed.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_mention_in_pr_review_comment_dispatches(self):
        pr_comment = _comment(3, "@deile-one please review this", kind="pr_review")
        monitor, github, notifier = _make_monitor(pr_comments=[pr_comment])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_both_issue_and_pr_comments_polled(self):
        ic = _comment(10, "@deile-one issue mention")
        pc = _comment(11, "@deile-one pr mention", kind="pr_review")
        monitor, github, notifier = _make_monitor(issue_comments=[ic], pr_comments=[pc])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 2
        assert notifier.mention_processed.call_count == 2

    async def test_claude_run_fails_mention_not_counted(self):
        """When claude.run returns non-ok, the mention is NOT counted."""
        comment = _comment(4, "@deile-one do something")
        monitor, github, notifier = _make_monitor(issue_comments=[comment], claude_ok=False)
        await monitor._process_mentions()
        notifier.mention_processed.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_cursor_saved_after_processing(self, tmp_path):
        """After _process_mentions(), the cursor file must exist."""
        cfg = PipelineConfig(
            repo="owner/name",
            base_repo_path=tmp_path,
            notify_user_id="42",
        )
        github = MagicMock()
        github.list_issue_comments_since = AsyncMock(return_value=[])
        github.list_pr_review_comments_since = AsyncMock(return_value=[])
        notifier = MagicMock()
        for attr in ("mention_processed", "error"):
            setattr(notifier, attr, AsyncMock())
        claude = MagicMock()
        claude.run = AsyncMock(return_value=ClaudeRunResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0, cmd=("claude",)
        ))
        monitor = PipelineMonitor(cfg, github=github, worktrees=MagicMock(), claude=claude, notifier=notifier)
        await monitor._process_mentions()
        assert monitor._mention_cursor_path.exists()

    async def test_cursor_case_insensitive_match(self):
        """Mention matching must be case-insensitive."""
        comment = _comment(5, "Hello @DEILE-ONE, please help")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1

    async def test_poll_exception_does_not_crash(self):
        """Exception during GitHub poll must be caught; loop continues cleanly."""
        monitor, github, notifier = _make_monitor()
        github.list_issue_comments_since = AsyncMock(side_effect=RuntimeError("network error"))
        # Should not raise
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_mention_handling_disabled_skips_on_tick(self):
        """When enable_mention_handling=False, _process_mentions is not called on tick."""
        comment = _comment(6, "@deile-one ignored")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        monitor.config.enable_mention_handling = False
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        await monitor.tick()
        github.list_issue_comments_since.assert_not_called()


# ----- Multi-trigger mention handling (issue #253) ------------------------

def _issue_ref(number=100):
    return IssueRef(
        number=number, title="test", url=f"https://github.com/o/r/issues/{number}",
        labels=(),
    )


def _pr_ref(number=200):
    return PrRef(
        number=number, title="pr", url=f"https://github.com/o/r/pull/{number}",
        labels=(), head_ref=f"auto/issue-{number}",
    )


class TestProcessMentionsMultiTrigger:
    async def test_assignee_issue_dispatches(self):
        """When DEILE is assigned to an issue, a mention dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(42)])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_assignee_pr_dispatches(self):
        """When DEILE is assigned to a PR, a mention dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_reviewer_request_dispatches(self):
        """When DEILE is requested as reviewer, a mention dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_with_review_requests = AsyncMock(return_value=[_pr_ref(88)])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_body_mention_issue_dispatches(self):
        """When @deile-one appears in an issue body, a dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(
            return_value=([_issue_ref(55)], [])
        )
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_body_mention_pr_dispatches(self):
        """When @deile-one appears in a PR body, a dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(
            return_value=([], [_pr_ref(66)])
        )
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_dedup_assignee_plus_comment_same_issue(self):
        """Assignee + mention on the SAME issue = single dispatch with full context."""
        comment = _comment(50, "Hey @deile-one fix this")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(1)])
        await monitor._process_mentions()
        # Both triggers target issue #1 → deduped into ONE dispatch
        assert monitor.stats.mentions_processed == 1
        notifier.mention_processed.assert_called_once()

    async def test_dedup_two_comments_same_issue(self):
        """Two @deile-one comments on the same issue = single dispatch."""
        c1 = _comment(100, "@deile-one do X")
        c2 = _comment(101, "@deile-one also Y")
        monitor, github, notifier = _make_monitor(issue_comments=[c1, c2])
        await monitor._process_mentions()
        # Both comments target issue #1 → deduped
        assert monitor.stats.mentions_processed == 1

    async def test_no_dedup_different_issues(self):
        """Mentions on different issues = separate dispatches."""
        c1 = _comment(200, "@deile-one", author="a")
        c2 = _comment(201, "@deile-one", author="b")
        # Change issue_url so they point to different issues
        c2 = CommentRef(
            comment_id=201, body="@deile-one",
            html_url="https://github.com/o/r/issues/2#issuecomment-201",
            issue_url="https://api.github.com/repos/o/r/issues/2",
            author="b", kind="issue",
        )
        monitor, github, notifier = _make_monitor(issue_comments=[c1, c2])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 2

    async def test_assignee_exception_does_not_crash(self):
        """Exception polling assignee must not crash the mention loop."""
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(side_effect=RuntimeError("boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_reviewer_exception_does_not_crash(self):
        """Exception polling reviewers must not crash the mention loop."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_with_review_requests = AsyncMock(side_effect=RuntimeError("boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_body_search_exception_does_not_crash(self):
        """Exception searching bodies must not crash the mention loop."""
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(side_effect=RuntimeError("boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_cursor_saved_with_new_triggers(self, tmp_path):
        """Cursor must be saved even when new trigger types are used."""
        cfg = PipelineConfig(
            repo="owner/name",
            base_repo_path=tmp_path,
            notify_user_id="42",
        )
        github = MagicMock()
        github.list_issue_comments_since = AsyncMock(return_value=[])
        github.list_pr_review_comments_since = AsyncMock(return_value=[])
        github.list_issues_assigned_to = AsyncMock(return_value=[])
        github.list_prs_assigned_to = AsyncMock(return_value=[])
        github.list_prs_with_review_requests = AsyncMock(return_value=[])
        github.search_items_mentioning = AsyncMock(return_value=([], []))
        notifier = MagicMock()
        for attr in ("mention_processed", "error"):
            setattr(notifier, attr, AsyncMock())
        claude = MagicMock()
        claude.run = AsyncMock(return_value=ClaudeRunResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0, cmd=("claude",)
        ))
        monitor = PipelineMonitor(cfg, github=github, worktrees=MagicMock(), claude=claude, notifier=notifier)
        await monitor._process_mentions()
        assert monitor._mention_cursor_path.exists()
