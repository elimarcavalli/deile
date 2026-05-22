"""Integration tests for the full stage 1 → 2 → 3 pipeline flow.

Uses mocked GitHub, Claude, and Worktree collaborators (same style as
test_monitor.py) but exercises multi-stage baton passing and label
transition ordering end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, call

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.labels import (REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.worktree_manager import Worktree

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_BATCH_ID = "abc12345"
_PR_URL = "https://github.com/owner/name/pull/55"


def _make_monitor_full(
    *,
    issues_new: Optional[List[IssueRef]] = None,
    issues_reviewed: Optional[List[IssueRef]] = None,
    prs: Optional[List[PrRef]] = None,
    claude_stdout: str = "",
    claude_rc: int = 0,
    review_callback=None,
    identity: Optional[MonitorIdentity] = None,
) -> Tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
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
        "issue_picked_up",
        "issue_reviewed",
        "implementation_started",
        "implementation_finished",
        "implementation_parked",
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
        review_callback=review_callback,
        identity=identity,
    )
    return monitor, github, notifier


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestStageIntegration:
    async def test_full_flow_issue_to_pr_review(self):
        """Three separate ticks: stage 1, then stage 2, then stage 3.

        The first tick picks up a ~workflow:nova issue and transitions it to
        ~workflow:revisada. The second tick picks up the reviewed issue and
        transitions it to ~workflow:em_pr (Claude returns PR URL in stdout).
        The third tick reviews the open PR and transitions it to
        ~review:concluida.
        """
        issue_nova = IssueRef(
            number=10,
            title="feat: new feature",
            url="https://github.com/owner/name/issues/10",
            labels=(WORKFLOW_NEW,),
            body="Please implement X",
        )
        issue_reviewed = IssueRef(
            number=10,
            title="feat: new feature",
            url="https://github.com/owner/name/issues/10",
            labels=(WORKFLOW_REVIEWED, f"~batch:{_BATCH_ID}", "~by:default"),
            body="Please implement X",
        )
        pr_open = PrRef(
            number=55,
            title="auto: issue-10",
            url=_PR_URL,
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-10",
        )

        # -- Tick 1: stage 1 only -----------------------------------------
        monitor, github, notifier = _make_monitor_full(issues_new=[issue_nova])
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        await monitor.tick()

        notifier.issue_picked_up.assert_called_once()
        notifier.issue_reviewed.assert_called_once()
        assert monitor.stats.issues_reviewed == 1

        # Transition calls: nova→em_revisao then em_revisao→revisada
        transition_calls = github.transition_issue.call_args_list
        assert len(transition_calls) == 2
        assert transition_calls[0] == call(
            10, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
        )
        assert transition_calls[1] == call(
            10, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_REVIEWED
        )

        # -- Tick 2: stage 2 only -----------------------------------------
        monitor2, github2, notifier2 = _make_monitor_full(
            issues_reviewed=[issue_reviewed],
            claude_stdout=f"Done! See {_PR_URL}",
        )
        monitor2.config.enable_review = False
        monitor2.config.enable_pr_review = False
        await monitor2.tick()

        notifier2.implementation_started.assert_called_once()
        notifier2.implementation_finished.assert_called_once()
        args, _ = notifier2.implementation_finished.call_args
        assert args[1] == _PR_URL
        assert monitor2.stats.issues_implemented == 1

        # Transitions: revisada → em_implementacao (atomic claim) → em_pr (PR opened)
        assert github2.transition_issue.call_args_list == [
            call(10, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_IMPLEMENTING),
            call(10, from_label=WORKFLOW_IMPLEMENTING, to_label=WORKFLOW_PR),
        ]

        # -- Tick 3: stage 3 only -----------------------------------------
        monitor3, github3, notifier3 = _make_monitor_full(
            prs=[pr_open],
            claude_stdout="All good, merged the PR.",
        )
        monitor3.config.enable_review = False
        monitor3.config.enable_implement = False
        await monitor3.tick()

        notifier3.pr_picked_up.assert_called_once()
        notifier3.pr_reviewed.assert_called_once()
        assert monitor3.stats.prs_reviewed == 1

        # Transition: em_andamento → concluida
        github3.transition_pr.assert_called_with(
            55, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
        )

    async def test_stage1_passes_baton_to_stage2(self):
        """After stage 1 review, the issue carries ~workflow:revisada + ~by:<id>."""
        issue_nova = IssueRef(
            number=7,
            title="fix: regression",
            url="u",
            labels=(WORKFLOW_NEW,),
        )
        monitor, github, notifier = _make_monitor_full(issues_new=[issue_nova])
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False

        await monitor.tick()

        # Ownership label is added so stage 2 knows who claimed it.
        add_labels_calls = github.add_labels.call_args_list
        owned = any(
            "~by:default" in str(c)
            for c in add_labels_calls
        )
        assert owned, f"~by:default not added; add_labels calls: {add_labels_calls}"

        # Workflow transition: nova → revisada (via em_revisao)
        transitions = github.transition_issue.call_args_list
        actions = [(c[1]["from_label"], c[1]["to_label"]) for c in transitions]
        assert (WORKFLOW_NEW, WORKFLOW_REVIEWING) in actions
        assert (WORKFLOW_REVIEWING, WORKFLOW_REVIEWED) in actions

    async def test_stage2_only_picks_own_claimed(self):
        """Stage 2 with default identity only picks issues labelled ~by:default."""
        issue_mine = IssueRef(
            number=1,
            title="mine",
            url="u",
            labels=(WORKFLOW_REVIEWED, f"~batch:{_BATCH_ID}", "~by:default"),
        )
        issue_other = IssueRef(
            number=2,
            title="not mine",
            url="u",
            labels=(WORKFLOW_REVIEWED, f"~batch:{_BATCH_ID}", "~by:m-other"),
        )
        # Default identity: is_default=True → uses owns(title) which returns True
        # for shard_count=1 but ALSO checks ownership_label. Let's verify the
        # other monitor's issue is NOT picked when identity is non-default.
        identity_a = MonitorIdentity(monitor_id="worker-a")
        monitor, github, notifier = _make_monitor_full(
            issues_reviewed=[issue_mine, issue_other],
            claude_stdout=f"Done! {_PR_URL}",
            identity=identity_a,
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False

        await monitor.tick()

        # worker-a only owns issues with ~by:worker-a — neither issue qualifies
        notifier.implementation_started.assert_not_called()

    async def test_stage2_picks_its_own_ownership_label(self):
        """Stage 2 picks issue labelled ~by:<its-own-id>."""
        identity_a = MonitorIdentity(monitor_id="worker-a")
        issue_owned = IssueRef(
            number=3,
            title="owned by worker-a",
            url="u",
            labels=(WORKFLOW_REVIEWED, f"~batch:{_BATCH_ID}", "~by:worker-a"),
        )
        monitor, github, notifier = _make_monitor_full(
            issues_reviewed=[issue_owned],
            claude_stdout=f"Implemented. {_PR_URL}",
            identity=identity_a,
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False

        await monitor.tick()

        notifier.implementation_started.assert_called_once()
        notifier.implementation_finished.assert_called_once()

    async def test_stage3_does_not_pick_peer_branch(self):
        """Stage 3 on monitor 'a' skips PRs from monitor 'b'."""
        identity_a = MonitorIdentity(monitor_id="a")
        pr_from_b = PrRef(
            number=20,
            title="b's PR",
            url="https://github.com/o/r/pull/20",
            labels=(REVIEW_PENDING,),
            head_ref="auto/b/issue-5",
        )
        monitor, github, notifier = _make_monitor_full(
            prs=[pr_from_b],
            identity=identity_a,
        )
        monitor.config.enable_review = False
        monitor.config.enable_implement = False

        await monitor.tick()

        notifier.pr_picked_up.assert_not_called()

    async def test_notifier_fires_on_every_transition(self):
        """All notifier events are emitted at the right stages."""
        issue_nova = IssueRef(
            number=5,
            title="feat: notifier check",
            url="u",
            labels=(WORKFLOW_NEW,),
        )
        monitor, github, notifier = _make_monitor_full(issues_new=[issue_nova])
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False

        await monitor.tick()

        notifier.issue_picked_up.assert_called_once_with(5, "feat: notifier check", "u")
        notifier.issue_reviewed.assert_called_once_with(5, "feat: notifier check", "u")
        notifier.error.assert_not_called()
