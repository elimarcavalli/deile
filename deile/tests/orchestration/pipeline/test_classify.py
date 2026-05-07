"""Tests for Stage 0: auto-classification of newly opened issues.

Covers:
- GitHubClient.list_unclassified_issues() — filtering logic
- PipelineMonitor._classify_new_issues() — Stage 0 behavior
- DiscordNotifier.issue_auto_classified() — notification message
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import (GhCommandError,
                                                        GitHubClient, IssueRef)
from deile.orchestration.pipeline.labels import WORKFLOW_NEW
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.notifier import DiscordNotifier

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _issue(number: int, labels: tuple, body: str = "filled body") -> IssueRef:
    return IssueRef(
        number=number,
        title=f"issue {number}",
        url=f"https://github.com/o/r/issues/{number}",
        labels=labels,
        body=body,
    )


def _make_monitor(*, unclassified: list | None = None) -> tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_unclassified_issues = AsyncMock(return_value=list(unclassified or []))
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "pr_picked_up", "pr_reviewed",
        "issue_auto_classified", "error",
    ):
        setattr(notifier, attr, AsyncMock())

    worktrees = MagicMock()

    claude = MagicMock()
    claude.run = AsyncMock(
        return_value=ClaudeRunResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0, cmd=("claude", "-p", "x")
        )
    )

    monitor = PipelineMonitor(cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier)
    return monitor, github, notifier


# ---------------------------------------------------------------------------
# GitHubClient.list_unclassified_issues
# ---------------------------------------------------------------------------

class TestListUnclassifiedIssues:
    async def test_returns_issues_without_pipeline_labels(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {
                "number": 1,
                "title": "plain intent",
                "url": "https://github.com/o/r/issues/1",
                "labels": [{"name": "intent"}],
                "body": "some body",
                "state": "open",
            },
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_issues()
        assert len(result) == 1
        assert result[0].number == 1

    async def test_filters_out_issues_with_workflow_label(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {
                "number": 2,
                "title": "already in pipeline",
                "url": "https://github.com/o/r/issues/2",
                "labels": [{"name": "intent"}, {"name": "~workflow:nova"}],
                "body": "body",
                "state": "open",
            },
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_issues()
        assert result == []

    async def test_filters_out_issues_with_batch_label(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {
                "number": 3,
                "title": "batch locked",
                "url": "https://github.com/o/r/issues/3",
                "labels": [{"name": "bug"}, {"name": "~batch:abc12345"}],
                "body": "body",
                "state": "open",
            },
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_issues()
        assert result == []

    async def test_filters_out_issues_with_review_label(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {
                "number": 4,
                "title": "under review",
                "url": "https://github.com/o/r/issues/4",
                "labels": [{"name": "intent"}, {"name": "~review:pendente"}],
                "body": "body",
                "state": "open",
            },
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_issues()
        assert result == []

    async def test_returns_empty_on_empty_output(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            result = await client.list_unclassified_issues()
        assert result == []

    async def test_mixed_returns_only_unclassified(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {
                "number": 10,
                "title": "eligible",
                "url": "u",
                "labels": [{"name": "intent"}],
                "body": "body",
                "state": "open",
            },
            {
                "number": 11,
                "title": "already classified",
                "url": "u",
                "labels": [{"name": "intent"}, {"name": "~workflow:nova"}],
                "body": "body",
                "state": "open",
            },
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_issues()
        assert len(result) == 1
        assert result[0].number == 10


# ---------------------------------------------------------------------------
# PipelineMonitor._classify_new_issues (Stage 0)
# ---------------------------------------------------------------------------

class TestClassifyNewIssues:
    async def test_no_unclassified_issues_noop(self):
        monitor, github, notifier = _make_monitor(unclassified=[])
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()
        notifier.issue_auto_classified.assert_not_called()

    async def test_classifies_intent_issue_with_body(self):
        issue = _issue(5, ("intent",), body="Intent description")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_called_once_with("issue", 5, [WORKFLOW_NEW])
        github.comment_on_issue.assert_called_once()
        notifier.issue_auto_classified.assert_called_once_with(5, issue.title, issue.url)

    async def test_classifies_bug_issue(self):
        issue = _issue(6, ("bug",), body="Bug description")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_called_once()

    async def test_skips_infra_issue(self):
        issue = _issue(7, ("intent", "infra"), body="Infra issue")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()

    async def test_skips_issue_with_empty_body(self):
        issue = _issue(8, ("intent",), body="")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()

    async def test_skips_issue_with_whitespace_only_body(self):
        issue = _issue(9, ("intent",), body="   \n\t  ")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()

    async def test_skips_issue_with_no_classifiable_label(self):
        issue = _issue(10, ("question", "help-wanted"), body="some body")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()

    async def test_gh_error_does_not_crash(self):
        monitor, github, notifier = _make_monitor()
        github.list_unclassified_issues = AsyncMock(
            side_effect=GhCommandError(("gh",), 1, "", "network error")
        )
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()

    async def test_add_labels_failure_continues_loop(self):
        issues = [
            _issue(20, ("intent",), body="body 1"),
            _issue(21, ("intent",), body="body 2"),
        ]
        monitor, github, notifier = _make_monitor(unclassified=issues)

        async def _add_labels_by_issue(kind, num, labels):
            if num == 20:
                raise RuntimeError("network")

        github.add_labels = AsyncMock(side_effect=_add_labels_by_issue)
        await monitor._classify_new_issues()
        notifier.error.assert_called_once()
        notifier.issue_auto_classified.assert_called_once_with(21, issues[1].title, issues[1].url)
        github.comment_on_issue.assert_called_once()

    async def test_comment_failure_does_not_trigger_error_notification(self):
        issue = _issue(22, ("intent",), body="body")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        github.comment_on_issue = AsyncMock(side_effect=RuntimeError("timeout"))
        await monitor._classify_new_issues()
        github.add_labels.assert_called_once()
        notifier.issue_auto_classified.assert_called_once()
        notifier.error.assert_not_called()

    async def test_disabled_classify_skips_stage0(self):
        issue = _issue(30, ("intent",), body="body")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        monitor.config.enable_classify = False
        await monitor.tick()
        github.list_unclassified_issues.assert_not_called()

    async def test_tick_calls_classify_before_review(self):
        issue = _issue(40, ("intent",), body="body")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        call_order = []
        github.list_unclassified_issues.side_effect = lambda **_: call_order.append("classify") or []
        github.list_issues_with_label.side_effect = lambda *a, **_: call_order.append("review") or []
        await monitor.tick()
        assert call_order.index("classify") < call_order.index("review")

    async def test_classify_via_scheduled_action(self):
        from datetime import datetime, timezone

        from deile.orchestration.pipeline.scheduler import PendingRun
        issue = _issue(50, ("intent",), body="body")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        run = PendingRun(
            when=datetime.now(timezone.utc),
            entry_id="x",
            action="classify",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        github.list_unclassified_issues.assert_called_once()


# ---------------------------------------------------------------------------
# DiscordNotifier.issue_auto_classified
# ---------------------------------------------------------------------------

class TestIssueAutoClassifiedNotification:
    async def test_sends_dm_with_issue_number(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="42", dm_fn=fake_dm)
        await n.issue_auto_classified(99, "My Issue", "https://github.com/o/r/issues/99")
        assert len(sent) == 1
        assert "#99" in sent[0]
        assert "My Issue" in sent[0]
        assert "~workflow:nova" in sent[0]

    async def test_disabled_notifier_noops(self):
        sent = []

        async def fake_dm(uid, text):
            sent.append(text)

        n = DiscordNotifier(user_id="", dm_fn=fake_dm)
        await n.issue_auto_classified(1, "t", "u")
        assert sent == []


# ---------------------------------------------------------------------------
# Parametrized: all classifiable labels are handled
# ---------------------------------------------------------------------------

class TestClassifiableLabelCoverage:
    @pytest.mark.parametrize("label", ["intent", "bug", "refactor", "feature_request"])
    async def test_classifies_each_classifiable_label(self, label):
        issue = _issue(99, (label,), body="some body")
        monitor, github, _ = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_called_once_with("issue", 99, [WORKFLOW_NEW])


# ---------------------------------------------------------------------------
# Scheduler integration: RecurringEntry with action="classify" is valid
# ---------------------------------------------------------------------------

class TestSchedulerClassifyAction:
    def test_recurring_classify_entry_is_valid(self):
        from deile.orchestration.pipeline.scheduler import (RecurringEntry,
                                                            Schedule)

        entry = RecurringEntry(id="cls-loop", action="classify", cron="*/5 * * * *")
        s = Schedule()
        s.add_recurring(entry)
        assert any(e.action == "classify" for e in s.recurring)
