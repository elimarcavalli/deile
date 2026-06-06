"""Integration tests: tick calls run_terminal_gc when reconcile detects closed items (issue #590).

These tests assert the WIRE-UP: that the pipeline calls run_terminal_gc when
reconcile_review_prs sees a merged PR, and when reconcile_closed_issues sees a
closed issue. This is the integration gap left by #587, which only tested the
function in isolation.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.forge.refs import IssueRef, PrRef
from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.implementer import WorkerImplementer
from deile.orchestration.pipeline.labels import (
    REVIEW_IN_PROGRESS,
    WORKFLOW_PR,
)
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor


_NOTIFIER_METHODS = (
    "issue_picked_up", "issue_reviewed", "implementation_started",
    "implementation_finished", "implementation_parked", "implementation_resumed",
    "implementation_blocked", "pr_picked_up", "pr_reviewed",
    "issue_auto_classified", "follow_ups_processed", "error",
    "pr_auto_classified", "mention_processed",
)


def _ledger_path() -> Path:
    return Path(tempfile.mkdtemp(prefix=".test_gc_")) / "dispatches.json"


def _issue(number: int, *labels: str, state: str = "open") -> IssueRef:
    return IssueRef(
        number=number, title="t",
        url=f"https://github.com/owner/repo/issues/{number}",
        labels=tuple(labels), state=state,
    )


def _pr(number: int, *labels: str, state: str = "open", head_ref: str = "auto/issue-1") -> PrRef:
    return PrRef(
        number=number, title="t",
        url=f"https://github.com/owner/repo/pull/{number}",
        labels=tuple(labels), state=state, head_ref=head_ref,
    )


def _make_monitor(*, prs=None, issues_by_label=None):
    cfg = PipelineConfig(
        repo="owner/repo",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        dispatch_mode="deile_worker",
        enable_refinement_gate=False,
        max_parallel=2,
        enable_resume=False,
        enable_classify=False,
        enable_pr_triage=False,
        enable_mention_handling=False,
        enable_review=False,
        enable_implement=False,
        enable_pr_review=True,
    )
    forge = MagicMock()
    forge.ensure_pipeline_labels = AsyncMock()
    forge.list_open_prs = AsyncMock(return_value=list(prs or []))
    forge.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: list((issues_by_label or {}).get(label, []))
    )
    forge.get_pr = AsyncMock(return_value=None)
    forge.get_issue = AsyncMock(return_value=None)
    forge.transition_pr = AsyncMock()
    forge.transition_issue = AsyncMock()
    forge.clear_batch_label = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.remove_labels = AsyncMock()
    forge.claim_with_batch = AsyncMock(return_value="abc12345")

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    client = MagicMock()
    client.get_resume_info = AsyncMock(return_value={
        "last_completed_at": None, "last_is_error": None,
        "last_result_full": "", "last_result_summary": "",
        "claude_alive": True, "workdir_exists": True,
    })
    ledger = DispatchLedger(path=_ledger_path())
    monitor = PipelineMonitor(
        cfg, github=forge, notifier=notifier,
        implementer=WorkerImplementer(client=client, ledger=ledger),
    )
    return monitor, forge


class TestReconcileReviewPrsCallsGC:
    async def test_calls_run_terminal_gc_when_pr_merged(self):
        """reconcile_review_prs calls run_terminal_gc best-effort on merged PRs."""
        pr = _pr(99, REVIEW_IN_PROGRESS, "~by:default")
        monitor, forge = _make_monitor(prs=[pr])

        ledger = monitor.implementer._ledger
        ledger.record(DispatchLedger.key_for_pr(99), task_id="t-001", session_id="s-001")

        monitor.implementer._client.get_resume_info = AsyncMock(return_value={
            "last_completed_at": 1_700_000_000,
            "last_is_error": False,
            "last_result_full": "reviewed",
            "last_result_summary": "reviewed",
            "claude_alive": False,
            "workdir_exists": True,
        })
        forge.get_pr = AsyncMock(return_value=None)

        with patch(
            "deile.orchestration.pipeline.stages.run_terminal_gc",
            new_callable=AsyncMock,
        ) as mock_gc:
            mock_gc.return_value = "success"
            await monitor._reconcile_review_prs()

        mock_gc.assert_awaited_once_with(forge, "pr", 99, "merged")

    async def test_gc_failure_does_not_break_tick(self):
        """If run_terminal_gc raises, reconcile_review_prs continues normally."""
        pr = _pr(77, REVIEW_IN_PROGRESS)
        monitor, forge = _make_monitor(prs=[pr])

        ledger = monitor.implementer._ledger
        ledger.record(DispatchLedger.key_for_pr(77), task_id="t-gc-fail", session_id="s-002")

        monitor.implementer._client.get_resume_info = AsyncMock(return_value={
            "last_completed_at": 1_700_000_000,
            "last_is_error": False,
            "last_result_full": "done",
            "last_result_summary": "done",
            "claude_alive": False,
            "workdir_exists": True,
        })
        forge.get_pr = AsyncMock(return_value=None)

        with patch(
            "deile.orchestration.pipeline.stages.run_terminal_gc",
            new_callable=AsyncMock,
        ) as mock_gc:
            mock_gc.side_effect = Exception("API timeout")
            await monitor._reconcile_review_prs()

        mock_gc.assert_awaited_once()
        forge.transition_pr.assert_awaited()


class TestReconcileClosedIssues:
    async def test_calls_run_terminal_gc_when_issue_closed(self):
        """reconcile_closed_issues calls run_terminal_gc for closed em_pr issues."""
        issue = _issue(42, WORKFLOW_PR, state="open")
        monitor, forge = _make_monitor(issues_by_label={WORKFLOW_PR: [issue]})

        closed_issue = _issue(42, WORKFLOW_PR, state="closed")
        forge.get_issue = AsyncMock(return_value=closed_issue)

        with patch(
            "deile.orchestration.pipeline.stages.run_terminal_gc",
            new_callable=AsyncMock,
        ) as mock_gc:
            mock_gc.return_value = "success"
            await monitor._reconcile_closed_issues()

        mock_gc.assert_awaited_once_with(forge, "issue", 42, "closed")

    async def test_skips_open_issues(self):
        """reconcile_closed_issues does NOT call GC on still-open issues."""
        issue = _issue(10, WORKFLOW_PR, state="open")
        monitor, forge = _make_monitor(issues_by_label={WORKFLOW_PR: [issue]})

        open_issue = _issue(10, WORKFLOW_PR, state="open")
        forge.get_issue = AsyncMock(return_value=open_issue)

        with patch(
            "deile.orchestration.pipeline.stages.run_terminal_gc",
            new_callable=AsyncMock,
        ) as mock_gc:
            await monitor._reconcile_closed_issues()

        mock_gc.assert_not_awaited()

    async def test_gc_failure_does_not_break_loop(self):
        """GC failure for one issue does not abort processing of remaining issues."""
        issues = [
            _issue(11, WORKFLOW_PR, state="open"),
            _issue(12, WORKFLOW_PR, state="open"),
        ]
        monitor, forge = _make_monitor(issues_by_label={WORKFLOW_PR: issues})

        def _get_issue(number):
            return IssueRef(
                number=number, title="t", url="u",
                labels=(WORKFLOW_PR,), state="closed",
            )
        forge.get_issue = AsyncMock(side_effect=_get_issue)

        gc_calls = []

        async def _failing_gc(forge_arg, kind, number, state):
            gc_calls.append(number)
            if number == 11:
                raise Exception("network error")
            return "success"

        with patch(
            "deile.orchestration.pipeline.stages.run_terminal_gc",
            side_effect=_failing_gc,
        ):
            await monitor._reconcile_closed_issues()

        assert 11 in gc_calls
        assert 12 in gc_calls

    async def test_forge_list_failure_does_not_break_tick(self):
        """If list_issues_with_label raises, reconcile_closed_issues returns silently."""
        from deile.orchestration.forge import GhCommandError
        monitor, forge = _make_monitor()
        forge.list_issues_with_label = AsyncMock(
            side_effect=GhCommandError(["gh", "issue", "list"], 1, "", "list failed")
        )

        with patch(
            "deile.orchestration.pipeline.stages.run_terminal_gc",
            new_callable=AsyncMock,
        ) as mock_gc:
            await monitor._reconcile_closed_issues()

        mock_gc.assert_not_awaited()


class TestGCWiredIntoTick:
    async def test_reconcile_closed_issues_called_during_dispatch_stages(self):
        """_reconcile_closed_issues is called on every _dispatch_stages (issue #590 wire-up)."""
        monitor, forge = _make_monitor()
        forge.list_issues_with_label = AsyncMock(return_value=[])

        with patch.object(monitor, "_reconcile_closed_issues", new_callable=AsyncMock) as mock_reconcile:
            await monitor._dispatch_stages()

        mock_reconcile.assert_awaited_once()
