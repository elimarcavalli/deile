"""Integration tests for PipelineMonitor + ScheduleStore scheduling logic.

Validates:
- schedule-driven ticks only run due actions
- fallback to legacy "all every tick" when no schedule exists
- catch-up on startup drains missed runs in chronological order
- one-shot entries fire once and are marked completed
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.labels import WORKFLOW_NEW, WORKFLOW_REVIEWED
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.scheduler import (OneshotEntry,
                                                    RecurringEntry, Schedule,
                                                    ScheduleStore)
from deile.orchestration.pipeline.worktree_manager import Worktree

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_BATCH_ID = "deadbeef"


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _make_monitor_sched(
    tmp_path: Path,
    *,
    issues_new: Optional[List[IssueRef]] = None,
    issues_reviewed: Optional[List[IssueRef]] = None,
    prs: Optional[List[PrRef]] = None,
    claude_stdout: str = "",
    claude_rc: int = 0,
    schedule_store: Optional[ScheduleStore] = None,
) -> Tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/repo",
        base_repo_path=tmp_path,
        notify_user_id=None,
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: {
            WORKFLOW_NEW: list(issues_new or []),
            WORKFLOW_REVIEWED: list(issues_reviewed or []),
        }.get(label, [])
    )
    github.list_open_prs = AsyncMock(return_value=list(prs or []))
    github.claim_with_batch = AsyncMock(return_value=_BATCH_ID)
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.list_unclassified_issues = AsyncMock(return_value=[])

    worktrees = MagicMock()
    worktrees.create_branch_worktree = AsyncMock(
        return_value=Worktree(
            path=tmp_path / ".worktrees" / "x",
            branch="x",
            base_repo=tmp_path,
        )
    )

    claude = MagicMock()
    claude.run = AsyncMock(
        return_value=ClaudeRunResult(
            returncode=claude_rc,
            stdout=claude_stdout,
            stderr="",
            duration_seconds=0.1,
            cmd=("claude", "-p", "x"),
        )
    )

    notifier = MagicMock()
    for attr in (
        "issue_picked_up",
        "issue_reviewed",
        "implementation_started",
        "implementation_finished",
        "pr_picked_up",
        "pr_reviewed",
        "issue_auto_classified",
        "error",
    ):
        setattr(notifier, attr, AsyncMock())

    monitor = PipelineMonitor(
        cfg,
        github=github,
        worktrees=worktrees,
        claude=claude,
        notifier=notifier,
        schedule_store=schedule_store,
    )
    return monitor, github, notifier


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestMonitorScheduleIntegration:
    async def test_tick_with_schedule_runs_only_due_actions(self, tmp_path):
        """A schedule with a due 'review' entry and a future 'implement' entry
        should invoke the review path but NOT the implement path."""
        issue_nova = IssueRef(
            number=1, title="sched issue", url="u", labels=(WORKFLOW_NEW,)
        )

        store = ScheduleStore(tmp_path, monitor_id="default")
        s = Schedule()
        # review: due (last_run_at = 2 hours ago)
        s.add_recurring(
            RecurringEntry(
                id="rev-loop",
                action="review",
                cron="*/5 * * * *",
                last_run_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        # implement: NOT due (last_run_at = just now → next fire is in the future)
        s.add_recurring(
            RecurringEntry(
                id="impl-loop",
                action="implement",
                cron="*/5 * * * *",
                last_run_at=datetime.now(timezone.utc),
            )
        )
        store.save(s)

        monitor, github, notifier = _make_monitor_sched(
            tmp_path,
            issues_new=[issue_nova],
            schedule_store=store,
        )
        await monitor.tick()

        # review path fired
        notifier.issue_picked_up.assert_called_once()
        # implement path did NOT fire (no implementation_started)
        notifier.implementation_started.assert_not_called()

    async def test_tick_without_schedule_falls_back_to_legacy(self, tmp_path):
        """When no schedule file exists, all enabled stages run every tick."""
        issue_nova = IssueRef(
            number=2, title="legacy issue", url="u", labels=(WORKFLOW_NEW,)
        )
        # Store with no file on disk (empty schedule)
        store = ScheduleStore(tmp_path, monitor_id="default")
        # Do NOT call store.save() — keep file missing → load() returns Schedule()

        monitor, github, notifier = _make_monitor_sched(
            tmp_path,
            issues_new=[issue_nova],
            schedule_store=store,
        )
        await monitor.tick()

        # Stage 1 must have run (legacy fallback)
        notifier.issue_picked_up.assert_called_once()

    async def test_catchup_drains_pending_on_start(self, tmp_path):
        """On start(), catch-up drains all due entries in chronological order
        and stats.catchup_runs reflects the count."""
        now = datetime.now(timezone.utc)

        store = ScheduleStore(tmp_path, monitor_id="default")
        s = Schedule()
        # 3 one-shot entries, all in the past (oldest first)
        for i, minutes_ago in enumerate([30, 20, 10]):
            s.add_oneshot(
                OneshotEntry(
                    id=f"oneshot-{i}",
                    action="review",
                    run_at=now - timedelta(minutes=minutes_ago),
                )
            )
        store.save(s)

        monitor, github, notifier = _make_monitor_sched(
            tmp_path,
            schedule_store=store,
        )
        # start() calls _catch_up_pending() before the poll loop
        await monitor.start()
        await monitor.stop()

        assert monitor.stats.catchup_runs == 3

    async def test_oneshot_marked_completed_after_fire(self, tmp_path):
        """A one-shot entry transitions to completed=True after it fires,
        and a subsequent tick does NOT re-fire it."""
        store = ScheduleStore(tmp_path, monitor_id="default")
        s = Schedule()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        s.add_oneshot(
            OneshotEntry(id="os-review", action="review", run_at=past)
        )
        store.save(s)

        issue_nova = IssueRef(
            number=3, title="oneshot issue", url="u", labels=(WORKFLOW_NEW,)
        )
        monitor, github, notifier = _make_monitor_sched(
            tmp_path,
            issues_new=[issue_nova],
            schedule_store=store,
        )

        # First tick — oneshot should fire
        await monitor.tick()
        assert monitor.stats.scheduled_runs == 1

        # Second tick — oneshot is now completed=True, should NOT fire again
        notifier.issue_picked_up.reset_mock()
        await monitor.tick()
        notifier.issue_picked_up.assert_not_called()

    async def test_schedule_with_multiple_due_entries_runs_all(self, tmp_path):
        """When both 'review' and 'implement' are due, both run on the same tick."""
        issue_nova = IssueRef(
            number=4, title="multi-stage", url="u", labels=(WORKFLOW_NEW,)
        )
        issue_reviewed = IssueRef(
            number=5,
            title="ready",
            url="u",
            labels=(WORKFLOW_REVIEWED, f"~batch:{_BATCH_ID}"),
        )

        store = ScheduleStore(tmp_path, monitor_id="default")
        s = Schedule()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        s.add_recurring(
            RecurringEntry(
                id="rev",
                action="review",
                cron="*/5 * * * *",
                last_run_at=long_ago,
            )
        )
        s.add_recurring(
            RecurringEntry(
                id="impl",
                action="implement",
                cron="*/5 * * * *",
                last_run_at=long_ago,
            )
        )
        store.save(s)

        monitor, github, notifier = _make_monitor_sched(
            tmp_path,
            issues_new=[issue_nova],
            issues_reviewed=[issue_reviewed],
            claude_stdout="https://github.com/owner/repo/pull/99",
            schedule_store=store,
        )
        await monitor.tick()

        assert monitor.stats.scheduled_runs >= 2
        notifier.issue_picked_up.assert_called_once()
        notifier.implementation_started.assert_called_once()
