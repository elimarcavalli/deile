"""Regression tests for the 35 gaps fixed in issue #129.

Each test covers one or more of the numbered gaps.  No real GitHub or
Claude calls are made — all I/O is mocked with AsyncMock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import (
    GhCommandError, IssueRef, PrRef, compute_batch_id_for_number)
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.labels import (REVIEW_PENDING, WORKFLOW_NEW,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor,
                                                  _extract_pr_url)
from deile.orchestration.pipeline.notifier import DiscordNotifier
from deile.orchestration.pipeline.scheduler import (OneshotEntry,
                                                    RecurringEntry, Schedule,
                                                    ScheduleStore)
from deile.orchestration.pipeline.worktree_manager import Worktree

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _issue(number: int, labels: tuple, body: str = "filled body") -> IssueRef:
    return IssueRef(
        number=number,
        title=f"title-{number}",
        url=f"https://github.com/o/r/issues/{number}",
        labels=labels,
        body=body,
    )


def _pr(number: int, labels: tuple, head_ref: str = "auto/issue-1") -> PrRef:
    return PrRef(
        number=number,
        title=f"PR-{number}",
        url=f"https://github.com/o/r/pull/{number}",
        labels=labels,
        head_ref=head_ref,
    )


def _make_monitor(
    *,
    issues_new: Optional[List[IssueRef]] = None,
    issues_reviewed: Optional[List[IssueRef]] = None,
    prs: Optional[List[PrRef]] = None,
    claude_rc: int = 0,
    claude_stdout: str = "",
    unclassified: Optional[List[IssueRef]] = None,
) -> Tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/repo",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        use_pid_lock=False,
        # Reaper desligado em test default — adiciona round-trips ao
        # forge mock que poluem call_order/error counters legacy.
        # Tests do reaper ligam explicitamente.
        reaper_stale_seconds=0,
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
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.list_unclassified_issues = AsyncMock(return_value=list(unclassified or []))
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)
    github.clear_batch_label = AsyncMock()

    worktrees = MagicMock()
    worktrees.create_branch_worktree = AsyncMock(
        return_value=Worktree(
            path=Path("/tmp/fake/.worktrees/x"),
            branch="x",
            base_repo=Path("/tmp/fake"),
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
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up", "pr_reviewed",
        "issue_auto_classified", "follow_ups_processed", "error",
    ):
        setattr(notifier, attr, AsyncMock())

    monitor = PipelineMonitor(cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier)
    return monitor, github, notifier


# ---------------------------------------------------------------------------
# gap #1: schedule fallback for stages not in recurring
# ---------------------------------------------------------------------------

class TestGap1ScheduleFallback:
    async def test_stages_missing_from_recurring_run_as_legacy(self, tmp_path):
        """When schedule has recurring entries but only 'review', classify/implement/pr_review
        should run as legacy fallback."""
        store = ScheduleStore(tmp_path, monitor_id="default")
        s = Schedule()
        s.add_recurring(RecurringEntry(
            id="rev",
            action="review",
            cron="*/5 * * * *",
            last_run_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ))
        store.save(s)

        unclassified = [_issue(10, ("intent",), body="body")]
        cfg = PipelineConfig(
            repo="owner/repo",
            base_repo_path=tmp_path,
            use_pid_lock=False,
        )
        github = MagicMock()
        github.ensure_pipeline_labels = AsyncMock()
        github.list_issues_with_label = AsyncMock(return_value=[])
        github.list_open_prs = AsyncMock(return_value=[])
        github.list_unclassified_issues = AsyncMock(return_value=unclassified)
        github.claim_with_batch = AsyncMock(return_value="abc")
        github.add_labels = AsyncMock()
        github.comment_on_issue = AsyncMock()
        github.clear_batch_label = AsyncMock()
        github.transition_issue = AsyncMock()
        github.transition_pr = AsyncMock()
        notifier = MagicMock()
        for attr in ("issue_auto_classified", "error"):
            setattr(notifier, attr, AsyncMock())

        worktrees = MagicMock()
        worktrees.create_branch_worktree = AsyncMock(
            return_value=Worktree(path=tmp_path / "wt", branch="x", base_repo=tmp_path)
        )

        monitor = PipelineMonitor(
            cfg, github=github, notifier=notifier, worktrees=worktrees, schedule_store=store
        )
        await monitor.tick()
        # list_unclassified_issues was called via legacy fallback even though
        # "classify" is not in the recurring schedule
        github.list_unclassified_issues.assert_called_once()

    async def test_stages_present_in_recurring_do_not_double_run(self, tmp_path):
        """Stages WITH a recurring entry should NOT run again via legacy fallback."""
        store = ScheduleStore(tmp_path, monitor_id="default")
        s = Schedule()
        # Only classify, set last_run_at in the past so it's due
        s.add_recurring(RecurringEntry(
            id="cls",
            action="classify",
            cron="*/2 * * * *",
            last_run_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        store.save(s)

        cfg = PipelineConfig(
            repo="owner/repo",
            base_repo_path=tmp_path,
            use_pid_lock=False,
        )
        github = MagicMock()
        github.ensure_pipeline_labels = AsyncMock()
        github.list_issues_with_label = AsyncMock(return_value=[])
        github.list_open_prs = AsyncMock(return_value=[])
        github.list_unclassified_issues = AsyncMock(return_value=[])
        github.clear_batch_label = AsyncMock()
        notifier = MagicMock()
        for attr in ("error",):
            setattr(notifier, attr, AsyncMock())

        worktrees = MagicMock()
        worktrees.create_branch_worktree = AsyncMock(
            return_value=Worktree(path=tmp_path / "wt", branch="x", base_repo=tmp_path)
        )

        monitor = PipelineMonitor(
            cfg, github=github, notifier=notifier, worktrees=worktrees, schedule_store=store
        )
        await monitor.tick()
        # classify ran once (via schedule), not twice
        assert github.list_unclassified_issues.call_count == 1


# ---------------------------------------------------------------------------
# gap #4: "security" label is classifiable by default
# ---------------------------------------------------------------------------

class TestGap4SecurityLabel:
    async def test_security_label_is_classifiable(self):
        issue = _issue(1, ("security",), body="security concern")
        monitor, github, _ = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_called()


# ---------------------------------------------------------------------------
# gap #5: empty body accepted + reminder comment posted
# ---------------------------------------------------------------------------

class TestGap5EmptyBodyAccepted:
    async def test_empty_body_classified_with_reminder(self):
        issue = _issue(2, ("intent",), body="")
        monitor, github, notifier = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_called_with("issue", 2, [WORKFLOW_NEW])
        comment = github.comment_on_issue.call_args[0][1]
        assert "preencha" in comment.lower() or "vazio" in comment.lower()

    async def test_non_empty_body_gets_standard_comment(self):
        issue = _issue(3, ("intent",), body="has content")
        monitor, github, _ = _make_monitor(unclassified=[issue])
        await monitor._classify_new_issues()
        github.add_labels.assert_called_with("issue", 3, [WORKFLOW_NEW])
        comment = github.comment_on_issue.call_args[0][1]
        # Standard comment should not include the template reminder
        assert "preencha" not in comment.lower()


# ---------------------------------------------------------------------------
# gap #6: Stage 0 uses claim_with_batch
# ---------------------------------------------------------------------------

class TestGap6Stage0UsesClaim:
    """Gap #6: with PARALLEL monitors Stage 0 claims a ~batch: lock BEFORE
    labelling (TOCTOU mitigation). A SINGLE monitor skips the claim entirely so
    the lock label isn't added+removed in the same pass (timeline noise)."""

    async def test_single_monitor_labels_without_claim(self):
        issue = _issue(4, ("intent",), body="body")
        monitor, github, _ = _make_monitor(unclassified=[issue])  # default = 1 monitor
        await monitor._classify_new_issues()
        github.claim_with_batch.assert_not_called()
        github.add_labels.assert_called_once_with("issue", 4, [WORKFLOW_NEW])

    async def test_multi_monitor_claims_before_labelling(self):
        issue = _issue(4, ("intent",), body="body")
        monitor, github, _ = _make_monitor(unclassified=[issue])
        # Stub a 2-monitor identity that owns everything (decouple from title hash).
        monitor.identity = SimpleNamespace(shard_count=2, owns=lambda key: True)
        call_order = []
        github.claim_with_batch.side_effect = lambda *a, **_: call_order.append("claim") or "abc"
        github.add_labels.side_effect = lambda *a, **_: call_order.append("label")
        await monitor._classify_new_issues()
        assert "claim" in call_order
        assert call_order.index("claim") < call_order.index("label")

    async def test_multi_monitor_skips_when_claim_returns_none(self):
        issue = _issue(5, ("intent",), body="body")
        monitor, github, _ = _make_monitor(unclassified=[issue])
        monitor.identity = SimpleNamespace(shard_count=2, owns=lambda key: True)
        github.claim_with_batch = AsyncMock(return_value=None)
        await monitor._classify_new_issues()
        github.add_labels.assert_not_called()


# ---------------------------------------------------------------------------
# gap #7: Stage 2 accepts issue with ownership label even without ~batch:
# ---------------------------------------------------------------------------

class TestGap7Stage2AcceptsOwnershipLabel:
    async def test_implements_issue_with_ownership_label_no_batch(self):
        ownership = MonitorIdentity().ownership_label()
        rev = IssueRef(
            number=10, title="t", url="u",
            labels=(WORKFLOW_REVIEWED, ownership),  # ~by:default but no ~batch:
        )
        monitor, _, notifier = _make_monitor(issues_reviewed=[rev])
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_called_once()

    async def test_skips_issue_without_batch_or_ownership(self):
        rev = IssueRef(
            number=11, title="t", url="u",
            labels=(WORKFLOW_REVIEWED,),  # neither batch nor ownership
        )
        monitor, _, notifier = _make_monitor(issues_reviewed=[rev])
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_not_called()


# ---------------------------------------------------------------------------
# gap #8: enable_review_human_prs flag
# ---------------------------------------------------------------------------

class TestGap8ReviewHumanPrs:
    async def test_human_pr_reviewed_when_flag_set(self):
        pr = _pr(20, (REVIEW_PENDING,), head_ref="feat/human-branch")
        monitor, github, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_review_human_prs = True
        await monitor.tick()
        notifier.pr_picked_up.assert_called_once()

    async def test_human_pr_skipped_when_flag_not_set(self):
        pr = _pr(21, (REVIEW_PENDING,), head_ref="feat/human-branch")
        monitor, github, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        # enable_review_human_prs defaults to False
        await monitor.tick()
        notifier.pr_picked_up.assert_not_called()


# ---------------------------------------------------------------------------
# gap #9: Stage 3 clears ~batch: label after conclude
# ---------------------------------------------------------------------------

class TestGap9BatchClearedAfterPrReview:
    async def test_clear_batch_called_after_pr_review(self):
        pr = _pr(30, (REVIEW_PENDING,), head_ref="auto/issue-1")
        monitor, github, _ = _make_monitor(prs=[pr], claude_stdout="done")
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        github.clear_batch_label.assert_called_once_with("pr", 30)


# ---------------------------------------------------------------------------
# gap #10: compute_batch_id_for_number uses number, not title
# ---------------------------------------------------------------------------

class TestGap10BatchIdByNumber:
    def test_different_numbers_different_ids(self):
        a = compute_batch_id_for_number("issue", 1)
        b = compute_batch_id_for_number("issue", 2)
        assert a != b

    def test_same_number_same_id_deterministic(self):
        assert (
            compute_batch_id_for_number("issue", 42)
            == compute_batch_id_for_number("issue", 42)
        )

    def test_issue_and_pr_same_number_differ(self):
        assert (
            compute_batch_id_for_number("issue", 10)
            != compute_batch_id_for_number("pr", 10)
        )


# ---------------------------------------------------------------------------
# gap #13: Stage 1 atomicity — revert on failure
# ---------------------------------------------------------------------------

class TestGap13Stage1Atomic:
    async def test_review_failure_reverts_to_nova(self):
        """If the review step fails, the issue must be reverted from em_revisao to nova."""
        issue = IssueRef(number=99, title="t", url="u", labels=(WORKFLOW_NEW,))
        monitor, github, notifier = _make_monitor(issues_new=[issue])

        async def _fail_reviewing_transition(number, *, from_label, to_label):
            if from_label == WORKFLOW_REVIEWING and to_label == WORKFLOW_REVIEWED:
                raise GhCommandError(("issue", "edit"), 1, "", "simulated failure")

        github.transition_issue = AsyncMock(side_effect=_fail_reviewing_transition)

        await monitor._review_one_new_issue()

        # The issue should have been transitioned back to WORKFLOW_NEW
        revert_calls = [
            c for c in github.transition_issue.call_args_list
            if c.kwargs.get("to_label") == WORKFLOW_NEW
        ]
        assert revert_calls, "expected revert to ~workflow:nova on review failure"
        assert monitor.stats.errors > 0


# ---------------------------------------------------------------------------
# gap #14: _extract_pr_url uses last match
# ---------------------------------------------------------------------------

class TestGap14ExtractPrUrlLastMatch:
    def test_returns_last_url_when_multiple_present(self):
        text = (
            "see https://github.com/o/r/pull/1 for reference\n"
            "and https://github.com/o/r/pull/2 for context\n"
            "created https://github.com/o/r/pull/99"
        )
        assert _extract_pr_url(text) == "https://github.com/o/r/pull/99"

    def test_single_url_still_returned(self):
        assert _extract_pr_url("https://github.com/o/r/pull/42") == \
            "https://github.com/o/r/pull/42"

    def test_none_when_no_url(self):
        assert _extract_pr_url("no url here") is None


# ---------------------------------------------------------------------------
# gap #17/#18: GhCommandError → ERROR level + stats.gh_errors
# ---------------------------------------------------------------------------

class TestGap17_18GhErrorCounting:
    async def test_gh_error_in_review_increments_gh_errors(self):
        monitor, github, notifier = _make_monitor()
        github.list_issues_with_label = AsyncMock(
            side_effect=GhCommandError(("issue", "list"), 1, "", "timeout")
        )
        monitor.config.enable_classify = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        assert monitor.stats.gh_errors == 1
        assert monitor.stats.errors >= 1
        notifier.error.assert_called_once()

    async def test_gh_error_in_classify_increments_gh_errors(self):
        monitor, github, notifier = _make_monitor()
        github.list_unclassified_issues = AsyncMock(
            side_effect=GhCommandError(("issue", "list"), 1, "", "timeout")
        )
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        assert monitor.stats.gh_errors == 1

    async def test_claude_error_in_implement_increments_claude_errors(self):
        # Issue #373: fire-and-forget dispatch — claude_errors are no longer
        # incremented inline on the dispatch tick. The implement stage
        # dispatches and returns immediately; _finalize_implement_outcome
        # (which increments claude_errors) only runs on the RESUME path
        # (resume_in_progress_issues). The reconcile/reaper stages handle
        # error recovery on subsequent ticks.
        ownership = MonitorIdentity().ownership_label()
        rev = IssueRef(
            number=20, title="t", url="u",
            labels=(WORKFLOW_REVIEWED, ownership, "~batch:abc12345"),
        )
        monitor, github, notifier = _make_monitor(
            issues_reviewed=[rev], claude_rc=1
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        # Fire-and-forget: the issue is claimed (started notification fires)
        # but error counters are deferred to the resume/reconcile path.
        notifier.implementation_started.assert_called_once()
        assert monitor.stats.claude_errors == 0


# ---------------------------------------------------------------------------
# gap #19: DiscordNotifier warns once when dm_fn is None
# ---------------------------------------------------------------------------

class TestGap19NotifierWarnsOnce:
    async def test_warning_emitted_once_when_no_dm_fn(self, monkeypatch):
        import logging

        import deile.orchestration.pipeline.notifier as notifier_mod

        # Re-enable logging in case a previous test called logging.disable().
        logging.disable(logging.NOTSET)
        # Reset the module-level _DM_FN cache so we get a clean state
        # regardless of test ordering (a previous test may have already
        # resolved _DM_FN to a non-None value).
        monkeypatch.setattr(notifier_mod, "_DM_FN", None)
        notifier = DiscordNotifier(user_id="42", dm_fn=None)
        # Patch the resolver to always return None so no real DM fn is loaded.
        # Use patch on the logger directly (more robust than caplog against
        # logging.disable() side-effects from other tests in the suite).
        with patch(
            "deile.orchestration.pipeline.notifier._resolve_dm_function",
            return_value=None,
        ), patch("deile.orchestration.pipeline.notifier.logger") as mock_logger:
            await notifier._send("msg 1")
            await notifier._send("msg 2")

        # warning() should have been called exactly once
        assert mock_logger.warning.call_count == 1
        call_args = str(mock_logger.warning.call_args_list[0])
        assert "deilebot" in call_args.lower() or "dm function" in call_args.lower()


# ---------------------------------------------------------------------------
# gap #22: _owns_pr_branch logs warning on empty head_ref
# ---------------------------------------------------------------------------

class TestGap22EmptyHeadRef:
    def test_empty_head_ref_returns_false(self):
        monitor, _, _ = _make_monitor()
        assert not monitor._owns_pr_branch("", pr_number=55)

    async def test_pr_with_empty_head_ref_logged(self):
        pr = PrRef(number=55, title="t", url="u", labels=(REVIEW_PENDING,), head_ref="")
        monitor, github, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        # Patch the monitor logger directly (robust against logging.disable() side-effects
        # from other tests — see deile/tests/core/test_file_context_truncation.py).
        with patch("deile.orchestration.pipeline.monitor.logger") as mock_logger:
            await monitor._review_one_open_pr()
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        head_ref_warnings = [c for c in warning_calls if "head_ref" in c.lower()]
        assert head_ref_warnings, f"expected warning about empty head_ref; got: {warning_calls}"


# ---------------------------------------------------------------------------
# gap #25: gc_completed_oneshots removes old completed entries
# ---------------------------------------------------------------------------

class TestGap25GcCompletedOneshots:
    def test_removes_completed_entries_older_than_max_age(self):
        s = Schedule()
        old = OneshotEntry(
            id="old",
            action="review",
            run_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        old.completed = True
        s.oneshot.append(old)
        recent = OneshotEntry(
            id="recent",
            action="review",
            run_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        recent.completed = True
        s.oneshot.append(recent)
        removed = s.gc_completed_oneshots(max_age_days=7)
        assert removed == 1
        assert len(s.oneshot) == 1
        assert s.oneshot[0].id == "recent"

    def test_keeps_incomplete_entries_regardless_of_age(self):
        s = Schedule()
        not_done = OneshotEntry(
            id="nd",
            action="implement",
            run_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        s.oneshot.append(not_done)
        removed = s.gc_completed_oneshots(max_age_days=7)
        assert removed == 0


# ---------------------------------------------------------------------------
# gap #27: use_pid_lock defaults to True
# ---------------------------------------------------------------------------

class TestGap27PidLockDefault:
    def test_use_pid_lock_defaults_to_true(self):
        cfg = PipelineConfig(repo="o/r", base_repo_path=Path("/tmp/r"))
        assert cfg.use_pid_lock is True


# ---------------------------------------------------------------------------
# gap #29: GhCommandError.__str__ includes full subcommand
# ---------------------------------------------------------------------------

class TestGap29GhCommandErrorStr:
    def test_error_message_includes_full_subcommand(self):
        err = GhCommandError(("issue", "list", "--repo", "o/r"), 1, "", "timeout")
        msg = str(err)
        assert "issue" in msg
        assert "list" in msg

    def test_does_not_truncate_first_subcommand(self):
        err = GhCommandError(("pr", "create", "--head", "branch"), 1, "", "auth error")
        assert "pr create" in str(err)


# ---------------------------------------------------------------------------
# gap #33: classifiable_labels includes "security" by default
# ---------------------------------------------------------------------------

class TestGap33ClassifiableLabelsDefault:
    def test_security_in_default_classifiable_labels(self):
        cfg = PipelineConfig(repo="o/r", base_repo_path=Path("/tmp"))
        assert "security" in cfg.classifiable_labels


# ---------------------------------------------------------------------------
# gap #34: pipeline reset command
# ---------------------------------------------------------------------------

class TestGap34PipelineReset:
    async def test_reset_removes_batch_and_by_labels(self):
        from deile.commands.builtin.pipeline_command import _reset_issue

        issue = IssueRef(
            number=42, title="t", url="u",
            labels=("intent", "~batch:abc12345", "~by:default"),
        )
        monitor, github, _ = _make_monitor()
        github.get_issue = AsyncMock(return_value=issue)

        result = await _reset_issue(monitor, 42)
        assert result.success
        github.remove_labels.assert_called_once()
        removed = github.remove_labels.call_args[0][2]
        assert "~batch:abc12345" in removed
        assert "~by:default" in removed

    async def test_reset_noop_when_no_lock_labels(self):
        from deile.commands.builtin.pipeline_command import _reset_issue

        issue = IssueRef(
            number=43, title="t", url="u",
            labels=("intent", "bug"),
        )
        monitor, github, _ = _make_monitor()
        github.get_issue = AsyncMock(return_value=issue)

        result = await _reset_issue(monitor, 43)
        assert result.success
        github.remove_labels.assert_not_called()


# ---------------------------------------------------------------------------
# gap #35: log flush / schedule reflects actual stage runs
# ---------------------------------------------------------------------------

class TestGap35ScheduleCoversAllStages:
    def test_default_schedule_has_all_four_stages(self, tmp_path):
        """The default schedule file must have recurring entries for all 4 stages."""
        default_schedule_path = Path(__file__).parents[5] / "config" / "pipeline_schedule_default.yaml"
        if not default_schedule_path.exists():
            pytest.skip("config/pipeline_schedule_default.yaml not found")
        store = ScheduleStore(default_schedule_path.parent, monitor_id="default")
        schedule = store.load()
        actions = {e.action for e in schedule.recurring}
        assert "classify" in actions, "schedule missing 'classify'"
        assert "review" in actions, "schedule missing 'review'"
        assert "implement" in actions, "schedule missing 'implement'"
        assert "pr_review" in actions, "schedule missing 'pr_review'"


# ---------------------------------------------------------------------------
# Reviewer fixes: bugs found during code review
# ---------------------------------------------------------------------------

class TestGcCompletedOneshotsNoDuplicateCutoff:
    """gc_completed_oneshots had a dead first assignment to cutoff (dead code removed)."""

    def test_removes_entries_correctly_with_fixed_cutoff(self):
        from datetime import timedelta

        from deile.orchestration.pipeline.scheduler import (OneshotEntry,
                                                            Schedule)

        s = Schedule()
        old_completed = OneshotEntry(
            id="o1",
            action="implement",
            run_at=datetime.now(timezone.utc) - timedelta(days=30),
            completed=True,
        )
        recent_completed = OneshotEntry(
            id="o2",
            action="review",
            run_at=datetime.now(timezone.utc) - timedelta(days=1),
            completed=True,
        )
        pending = OneshotEntry(
            id="o3",
            action="classify",
            run_at=datetime.now(timezone.utc) + timedelta(hours=1),
            completed=False,
        )
        s.oneshot = [old_completed, recent_completed, pending]
        removed = s.gc_completed_oneshots(max_age_days=7)
        assert removed == 1
        assert len(s.oneshot) == 2
        remaining_ids = {o.id for o in s.oneshot}
        assert "o1" not in remaining_ids
        assert "o2" in remaining_ids
        assert "o3" in remaining_ids


class TestCleanupMergedBranchesNestedPath:
    """cleanup_merged_branches previously used candidate.name (last component only),
    which never matched full branch names like 'auto/issue-42'. Now uses relative_to()."""

    async def test_deletes_nested_worktree_when_branch_merged(self, tmp_path):
        from deile.orchestration.pipeline.worktree_manager import \
            WorktreeManager

        # Create a fake git repo
        (tmp_path / ".git").mkdir()
        wm = WorktreeManager(base_repo=tmp_path)

        # Simulate a worktree at .worktrees/auto/issue-42 with a .git marker
        wt_dir = tmp_path / ".worktrees" / "auto" / "issue-42"
        wt_dir.mkdir(parents=True)
        (wt_dir / ".git").mkdir()

        deleted = await wm.cleanup_merged_branches(["auto/issue-42"])

        assert deleted == 1
        assert not wt_dir.exists()

    async def test_no_delete_when_branch_not_merged(self, tmp_path):
        from deile.orchestration.pipeline.worktree_manager import \
            WorktreeManager

        (tmp_path / ".git").mkdir()
        wm = WorktreeManager(base_repo=tmp_path)

        wt_dir = tmp_path / ".worktrees" / "auto" / "issue-99"
        wt_dir.mkdir(parents=True)
        (wt_dir / ".git").mkdir()

        deleted = await wm.cleanup_merged_branches([])

        assert deleted == 0
        assert wt_dir.exists()
