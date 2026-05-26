"""Unit tests for PipelineMonitor — uses mocked GitHub/Claude/Worktree/Notifier."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.labels import (REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor,
                                                  _extract_pr_url)
from deile.orchestration.pipeline.worktree_manager import Worktree


def _make_monitor(
    *,
    issues_new: Optional[List[IssueRef]] = None,
    issues_reviewed: Optional[List[IssueRef]] = None,
    prs: Optional[List[PrRef]] = None,
    claude_stdout: str = "",
    claude_rc: int = 0,
    review_callback=None,
) -> Tuple[PipelineMonitor, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(side_effect=lambda label, **_: {
        WORKFLOW_NEW: list(issues_new or []),
        WORKFLOW_REVIEWED: list(issues_reviewed or []),
    }.get(label, []))
    github.list_open_prs = AsyncMock(return_value=list(prs or []))
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()

    worktrees = MagicMock()
    worktrees.create_branch_worktree = AsyncMock(
        return_value=Worktree(path=Path("/tmp/fake/.worktrees/x"),
                              branch="x", base_repo=Path("/tmp/fake"))
    )

    claude = MagicMock()
    claude.run = AsyncMock(return_value=ClaudeRunResult(
        returncode=claude_rc,
        stdout=claude_stdout,
        stderr="",
        duration_seconds=0.1,
        cmd=("claude", "-p", "x"),
    ))

    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)
    github.clear_batch_label = AsyncMock()
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up",
        "pr_reviewed", "issue_auto_classified", "follow_ups_processed", "error",
        "pr_auto_classified", "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=worktrees, claude=claude, notifier=notifier,
        review_callback=review_callback,
    )
    return monitor, notifier


class TestExtractPrUrl:
    def test_extracts_pr_url(self):
        assert _extract_pr_url("see https://github.com/o/r/pull/9") == \
            "https://github.com/o/r/pull/9"

    def test_returns_none_when_no_url(self):
        assert _extract_pr_url("nothing here") is None

    def test_handles_empty_string(self):
        assert _extract_pr_url("") is None


class TestStage1Review:
    async def test_no_new_issues_no_op(self):
        monitor, notifier = _make_monitor(issues_new=[])
        await monitor.tick()
        notifier.issue_picked_up.assert_not_called()

    async def test_picks_up_first_unclaimed_issue(self):
        new_issue = IssueRef(number=1, title="t", url="u", labels=(WORKFLOW_NEW,))
        monitor, notifier = _make_monitor(issues_new=[new_issue])
        await monitor.tick()
        notifier.issue_picked_up.assert_called_once()
        notifier.issue_reviewed.assert_called_once()
        assert monitor.stats.issues_reviewed == 1

    async def test_skips_already_claimed(self):
        claimed = IssueRef(
            number=1, title="t", url="u",
            labels=(WORKFLOW_NEW, "~batch:dead0000"),
        )
        monitor, notifier = _make_monitor(issues_new=[claimed])
        await monitor.tick()
        notifier.issue_picked_up.assert_not_called()

    async def test_review_callback_invoked(self):
        new_issue = IssueRef(number=1, title="t", url="u", labels=(WORKFLOW_NEW,))
        called: List[IssueRef] = []

        async def cb(i):
            called.append(i)
            return "review comment"

        monitor, notifier = _make_monitor(issues_new=[new_issue], review_callback=cb)
        await monitor.tick()
        assert called and called[0].number == 1
        monitor.github.comment_on_issue.assert_called_once_with(1, "review comment")


class TestStage2Implement:
    async def test_implements_reviewed_with_batch(self):
        rev = IssueRef(
            number=2, title="impl me", url="u",
            labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(
            issues_reviewed=[rev],
            claude_stdout="Done. https://github.com/owner/name/pull/3",
        )
        # Disable stage 1 and 3 to focus on stage 2.
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_called_once()
        notifier.implementation_finished.assert_called_once()
        # PR URL extracted from stdout
        args, kwargs = notifier.implementation_finished.call_args
        assert args[1] == "https://github.com/owner/name/pull/3"
        assert monitor.stats.issues_implemented == 1

    async def test_claims_before_notifying_and_completes_to_em_pr(self):
        # The claim (revisada → em_implementacao) MUST happen before the
        # "started" notification, and a successful PR moves em_implementacao →
        # em_pr. These two transitions are the lock + the completion marker.
        rev = IssueRef(
            number=2, title="impl me", url="u",
            labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(
            issues_reviewed=[rev],
            claude_stdout="Done. https://github.com/owner/name/pull/3",
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        calls = monitor.github.transition_issue.call_args_list
        # First transition is the atomic claim out of the candidate queue.
        assert calls[0].kwargs == {
            "from_label": WORKFLOW_REVIEWED, "to_label": WORKFLOW_IMPLEMENTING
        }
        # Last transition marks the PR as opened.
        assert calls[-1].kwargs == {
            "from_label": WORKFLOW_IMPLEMENTING, "to_label": WORKFLOW_PR
        }
        notifier.implementation_started.assert_called_once()

    async def test_skips_reviewed_without_batch(self):
        rev = IssueRef(
            number=2, title="t", url="u",
            labels=(WORKFLOW_REVIEWED,),  # no batch claim
        )
        monitor, notifier = _make_monitor(issues_reviewed=[rev])
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_not_called()

    async def test_already_claimed_issue_is_not_picked_again(self):
        # Regression for the duplicate-DM storm (#253): an issue already in
        # ~workflow:em_implementacao must NOT be re-selected even if it still
        # carries revisada-era labels. Without the claim guard the same issue
        # was re-dispatched every tick.
        claimed = IssueRef(
            number=2, title="t", url="u",
            labels=(WORKFLOW_REVIEWED, WORKFLOW_IMPLEMENTING, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(issues_reviewed=[claimed])
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_not_called()
        monitor.github.transition_issue.assert_not_called()

    async def test_no_pr_url_parks_without_retry(self):
        # ok=True but the agent opened no PR: park in em_implementacao + notify
        # once. Crucially it does NOT advance to em_pr and does NOT count as an
        # implemented issue — and is not re-tried (the issue left revisada).
        rev = IssueRef(
            number=2, title="vague meta issue", url="u",
            labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(
            issues_reviewed=[rev],
            claude_stdout="I thought about it but opened no PR.",
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_called_once()
        notifier.implementation_parked.assert_called_once()
        notifier.implementation_finished.assert_not_called()
        assert monitor.stats.issues_implemented == 0
        # Only the claim transition fired — never the em_pr completion.
        for call in monitor.github.transition_issue.call_args_list:
            assert call.kwargs.get("to_label") != WORKFLOW_PR

    async def test_claude_failure_parks_issue(self):
        rev = IssueRef(
            number=2, title="t", url="u",
            labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(
            issues_reviewed=[rev],
            claude_rc=2,
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        # A real failure parks the issue (one ping) instead of completing it.
        notifier.implementation_parked.assert_called_once()
        notifier.implementation_finished.assert_not_called()


class TestStage3PrReview:
    async def test_picks_up_unclaimed_open_pr(self):
        pr = PrRef(number=10, title="prt", url="https://x/pull/10",
                   labels=(REVIEW_PENDING,), head_ref="auto/issue-2")
        monitor, notifier = _make_monitor(prs=[pr], claude_stdout="merged.")
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_called_once()
        notifier.pr_reviewed.assert_called_once()
        assert monitor.stats.prs_reviewed == 1

    async def test_skips_drafts(self):
        pr = PrRef(number=10, title="t", url="u", labels=(),
                   head_ref="x", is_draft=True)
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_not_called()

    async def test_skips_concluded_prs(self):
        pr = PrRef(number=10, title="t", url="u",
                   labels=(REVIEW_CONCLUDED,), head_ref="x")
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_not_called()

    async def test_skips_in_progress_prs(self):
        pr = PrRef(number=10, title="t", url="u",
                   labels=(REVIEW_IN_PROGRESS,), head_ref="x")
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_not_called()


class TestLifecycle:
    async def test_start_then_stop_runs_at_least_one_tick(self):
        monitor, notifier = _make_monitor()
        monitor.config.poll_interval_seconds = 1
        await monitor.start()
        # Allow the first tick to fire.
        import asyncio
        await asyncio.sleep(0.05)
        await monitor.stop()
        assert monitor.stats.ticks >= 1
        monitor.github.ensure_pipeline_labels.assert_called_once()


# ---------------------------------------------------------------------------
# Multi-monitor identity-aware tests
# ---------------------------------------------------------------------------

from deile.orchestration.pipeline.identity import MonitorIdentity  # noqa: E402


class TestIdentityAwareSelection:
    async def test_default_identity_picks_any_issue(self, tmp_path):
        new_issue = IssueRef(number=1, title="t", url="u", labels=(WORKFLOW_NEW,))
        monitor, notifier = _make_monitor(issues_new=[new_issue])
        # default identity (shard_count=1) → owns everything
        await monitor.tick()
        notifier.issue_picked_up.assert_called_once()

    async def test_sharded_identity_skips_other_shard(self, tmp_path):
        # Pick a title that hashes to shard 1 (we'll make monitor be shard 0).
        # Iterate to find one.
        from deile.orchestration.pipeline.identity import MonitorIdentity
        a = MonitorIdentity(monitor_id="a", shard_index=0, shard_count=2)
        # Find a title that shard 0 does NOT own.
        title = None
        for i in range(1, 100):
            cand = f"some title {i}"
            if not a.owns(cand):
                title = cand
                break
        assert title is not None, "could not find unowned title"
        new_issue = IssueRef(number=1, title=title, url="u", labels=(WORKFLOW_NEW,))
        monitor, notifier = _make_monitor(issues_new=[new_issue])
        monitor.identity = a
        await monitor.tick()
        notifier.issue_picked_up.assert_not_called()

    async def test_branch_for_issue_uses_default_prefix(self):
        monitor, _ = _make_monitor()
        # default identity → legacy prefix
        assert monitor.branch_for_issue(42) == "auto/issue-42"

    async def test_branch_for_issue_uses_namespaced_prefix(self):
        monitor, _ = _make_monitor()
        monitor.identity = MonitorIdentity(monitor_id="m-alfa")
        assert monitor.branch_for_issue(42) == "auto/m-alfa/issue-42"

    async def test_pr_ownership_default_matches_legacy_prefix(self):
        monitor, _ = _make_monitor()
        assert monitor._owns_pr_branch("auto/issue-42")
        assert not monitor._owns_pr_branch("feat/something-else")

    async def test_pr_ownership_namespaced(self):
        monitor, _ = _make_monitor()
        monitor.identity = MonitorIdentity(monitor_id="m-alfa")
        assert monitor._owns_pr_branch("auto/m-alfa/issue-1")
        assert not monitor._owns_pr_branch("auto/m-beta/issue-1")
        assert not monitor._owns_pr_branch("auto/issue-1")  # legacy prefix not ours


# ---------------------------------------------------------------------------
# PID lock auto-enable for non-default identity
# ---------------------------------------------------------------------------

def _make_minimal_monitor(
    tmp_path,
    *,
    identity,
    use_pid_lock: bool = False,
):
    """Build a PipelineMonitor with all I/O mocked, using ``tmp_path`` as repo."""
    from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                      PipelineMonitor)
    from deile.orchestration.pipeline.worktree_manager import Worktree

    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=tmp_path,
        use_pid_lock=use_pid_lock,
        poll_interval_seconds=60,
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])

    worktrees = MagicMock()
    worktrees.create_branch_worktree = AsyncMock(
        return_value=Worktree(path=tmp_path / ".wt", branch="x", base_repo=tmp_path)
    )

    notifier = MagicMock()
    for attr in ("issue_picked_up", "issue_reviewed", "implementation_started",
                 "implementation_finished", "pr_picked_up", "pr_reviewed",
                 "issue_auto_classified", "follow_ups_processed", "error",
                 "pr_auto_classified", "mention_processed"):
        setattr(notifier, attr, AsyncMock())

    schedule_store = MagicMock()
    schedule_store.load = MagicMock(return_value=MagicMock(
        recurring=[], oneshot=[], compute_pending=MagicMock(return_value=[])
    ))

    return PipelineMonitor(
        cfg,
        github=github,
        worktrees=worktrees,
        notifier=notifier,
        identity=identity,
        schedule_store=schedule_store,
    )


class TestPidLockAutoEnable:
    async def test_non_default_identity_creates_lockfile(self, tmp_path):
        """A non-default identity must acquire a PID lock even when
        config.use_pid_lock is False (multi-monitor guard)."""
        from deile.orchestration.pipeline.identity import MonitorIdentity

        identity = MonitorIdentity(monitor_id="gamma")
        monitor = _make_minimal_monitor(tmp_path, identity=identity, use_pid_lock=False)
        try:
            await monitor.start()
            # After start(), the lockfile must exist under base_repo_path.
            lock_path = tmp_path / identity.lockfile_name()
            assert lock_path.exists(), f"expected lockfile at {lock_path}"
        finally:
            await monitor.stop()

    async def test_default_identity_no_pid_lock_flag_skips_lockfile(self, tmp_path):
        """Default identity with use_pid_lock=False must NOT create a lockfile."""
        from deile.orchestration.pipeline.identity import MonitorIdentity

        identity = MonitorIdentity()  # default
        monitor = _make_minimal_monitor(tmp_path, identity=identity, use_pid_lock=False)
        try:
            await monitor.start()
            # No lockfile should be created for the default identity without flag.
            lock_path = tmp_path / identity.lockfile_name()
            assert not lock_path.exists(), f"unexpected lockfile at {lock_path}"
        finally:
            await monitor.stop()


# ---------------------------------------------------------------------------
# Ownership label stamped on claimed PRs
# ---------------------------------------------------------------------------

class TestPrOwnershipLabel:
    async def test_claimed_pr_gets_ownership_label(self):
        """After a PR is claimed in stage 3, the monitor's ownership label must
        be stamped on the PR — mirroring stage 1 issue behaviour."""
        pr = PrRef(
            number=77, title="my pr", url="https://x/pull/77",
            labels=(REVIEW_PENDING,), head_ref="auto/issue-5",
        )
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()

        # ownership label must have been added
        ownership = monitor.identity.ownership_label()
        add_labels_calls = monitor.github.add_labels.call_args_list
        ownership_calls = [c for c in add_labels_calls if ownership in (c.args[2] if c.args else [])]
        assert ownership_calls, (
            f"expected add_labels call with {ownership!r}; calls were: {add_labels_calls}"
        )


# ---------------------------------------------------------------------------
# Stage 4: follow-up issue creation
# ---------------------------------------------------------------------------

class TestStage4FollowUps:
    def _merged_pr(self) -> PrRef:
        return PrRef(
            number=55, title="fix: parser", url="https://github.com/o/r/pull/55",
            labels=(REVIEW_PENDING,), head_ref="auto/issue-10",
        )

    async def test_stage4_not_called_when_not_merged(self):
        """If claude did not merge, stage 4 must not run."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="review done", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        monitor.github.get_pr_body.assert_not_called()

    async def test_stage4_not_called_when_disabled(self):
        """enable_follow_ups=False must suppress stage 4 entirely."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged ok", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_follow_ups = False
        await monitor.tick()
        monitor.github.get_pr_body.assert_not_called()

    async def test_stage4_runs_after_merge(self):
        """When claude stdout contains 'merged', stage 4 fetches PR body + comments."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="PR merged successfully", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        monitor.github.get_pr_body.assert_called_once_with(55)
        monitor.github.list_pr_comments.assert_called_once_with(55)

    async def test_stage4_opens_issue_for_non_breaking_followup(self):
        """Non-breaking follow-up items must be opened as issues with label 'intent'."""
        pr = self._merged_pr()
        monitor, notifier = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            return_value="## Follow-up\n- Write integration tests\n"
        )
        monitor.github.create_issue = AsyncMock(return_value=99)
        await monitor.tick()
        monitor.github.create_issue.assert_called_once()
        call_args = monitor.github.create_issue.call_args
        assert "Write integration tests" in call_args.args[0]
        assert call_args.kwargs.get("labels") == ["intent"]
        assert monitor._stats.follow_ups_opened == 1

    async def test_stage4_skips_breaking_change(self):
        """Breaking-change items must be skipped (not opened as issues)."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            return_value="## Follow-up\n- Breaking change: remove old API\n"
        )
        await monitor.tick()
        monitor.github.create_issue.assert_not_called()
        assert monitor._stats.follow_ups_skipped == 1

    async def test_stage4_comments_on_pr(self):
        """Stage 4 must post a follow-up report as a comment on the merged PR."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            return_value="## Follow-up\n- Add more tests\n"
        )
        monitor.github.create_issue = AsyncMock(return_value=42)
        await monitor.tick()
        monitor.github.comment_on_pr.assert_called()
        comment_body = monitor.github.comment_on_pr.call_args.args[1]
        assert "Stage 4" in comment_body

    async def test_stage4_no_followups_no_comment(self):
        """When no follow-ups are detected, no PR comment should be posted."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(return_value="Just a summary.")
        monitor.github.list_pr_comments = AsyncMock(return_value=[])
        await monitor.tick()
        monitor.github.comment_on_pr.assert_not_called()

    async def test_stage4_notifies_discord(self):
        """follow_ups_processed notification must fire after stage 4."""
        pr = self._merged_pr()
        monitor, notifier = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            return_value="## Follow-up\n- Improve error messages\n"
        )
        monitor.github.create_issue = AsyncMock(return_value=77)
        await monitor.tick()
        notifier.follow_ups_processed.assert_called_once_with(55, 1, 0)

    async def test_stage4_error_in_get_pr_body_does_not_propagate(self):
        """Failure in get_pr_body must not abort the tick — stage 3 result stands."""
        from deile.orchestration.pipeline.github_client import GhCommandError

        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            side_effect=GhCommandError(["gh"], 1, "", "network error")
        )
        await monitor.tick()
        assert monitor._stats.errors == 0

    async def test_stage4_create_issue_failure_counted_as_skipped(self):
        """If create_issue fails, the item is counted as skipped, not opened."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            return_value="## Follow-up\n- Refactor logger\n"
        )
        monitor.github.create_issue = AsyncMock(side_effect=Exception("gh error"))
        await monitor.tick()
        assert monitor._stats.follow_ups_opened == 0
        assert monitor._stats.follow_ups_skipped == 1

    async def test_stage4_all_breaking_no_issues_opened(self):
        """When every detected item is a breaking change, no issues are opened."""
        pr = self._merged_pr()
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged", claude_rc=0)
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.github.get_pr_body = AsyncMock(
            return_value=(
                "## Follow-up\n"
                "- Breaking change: drop Python 3.9 support\n"
                "- Incompatible API refactor\n"
            )
        )
        await monitor.tick()
        monitor.github.create_issue.assert_not_called()
        assert monitor._stats.follow_ups_opened == 0
        assert monitor._stats.follow_ups_skipped == 2


class TestForgeErrorCounter:
    """Testa o renomeamento gh_errors → forge_errors e o alias deprecated."""

    def test_forge_errors_starts_at_zero(self):
        """O campo forge_errors deve existir e iniciar em 0."""
        from deile.orchestration.pipeline.monitor import _Stats
        s = _Stats()
        assert s.forge_errors == 0

    def test_forge_errors_can_be_incremented(self):
        from deile.orchestration.pipeline.monitor import _Stats
        s = _Stats()
        s.forge_errors += 1
        assert s.forge_errors == 1

    def test_gh_errors_alias_reads_forge_errors(self):
        """``gh_errors`` deve retornar o valor de ``forge_errors``."""
        import warnings
        from deile.orchestration.pipeline.monitor import _Stats
        s = _Stats()
        s.forge_errors = 3
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert s.gh_errors == 3
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "forge_errors" in str(w[0].message)

    def test_gh_errors_alias_emits_deprecation_warning(self):
        """Ler ``gh_errors`` deve emitir DeprecationWarning."""
        import warnings
        from deile.orchestration.pipeline.monitor import _Stats
        s = _Stats()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = s.gh_errors
        assert any(issubclass(x.category, DeprecationWarning) for x in w)
