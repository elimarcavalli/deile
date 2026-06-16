"""Unit tests for PipelineMonitor — uses mocked GitHub/Claude/Worktree/Notifier."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.implementer import WorkOutcome
from deile.orchestration.pipeline.labels import (
    REVIEW_CONCLUDED,
    REVIEW_IN_PROGRESS,
    REVIEW_PENDING,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_NEW,
    WORKFLOW_PR,
    WORKFLOW_REVIEWED,
)
from deile.orchestration.pipeline.monitor import (
    PipelineConfig,
    PipelineMonitor,
    _extract_pr_url,
)
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
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()

    worktrees = MagicMock()
    worktrees.create_branch_worktree = AsyncMock(
        return_value=Worktree(
            path=Path("/tmp/fake/.worktrees/x"), branch="x", base_repo=Path("/tmp/fake")
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

    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)
    github.clear_batch_label = AsyncMock()
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.has_open_pr_for_issue = AsyncMock(return_value=False)

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
        "follow_ups_processed",
        "error",
        "pr_auto_classified",
        "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    # Issue #309 fase 2 + #373: ``build_implementer`` retorna ``WorkerImplementer``
    # e o pr_review fresh agora é fire-and-forget (dispatch num tick, veredito no
    # reconcile do tick seguinte por ground-truth). O stub abaixo mimetiza esse
    # contrato: ``review(resume=False)`` grava task_id no ledger e devolve 202;
    # ``_reconcile_review_prs`` consulta o ``_client.get_resume_info`` (done) e
    # decide por ground-truth (``get_pr`` None ⇒ merged). ``implement`` segue
    # fire-and-forget igual; o merge é sinalizado por ``claude_stdout``.
    implementer_stub = _FakeFireAndForgetImplementer(
        claude_stdout=claude_stdout,
        claude_rc=claude_rc,
    )

    monitor = PipelineMonitor(
        cfg,
        github=github,
        worktrees=worktrees,
        claude=claude,
        notifier=notifier,
        review_callback=review_callback,
        implementer=implementer_stub,
    )
    return monitor, notifier


class _FakeFireAndForgetImplementer:
    """Implementer fake compatível com o fluxo fire-and-forget (issue #373).

    - ``review(resume=False)`` / ``implement(resume=False)``: gravam um task_id no
      ledger e devolvem ``WorkOutcome(ok, task_id)`` SEM bloquear (mimetiza o
      dispatch 202). ``review(resume=True)`` devolve o outcome estruturado
      bloqueante (caminho resume preservado).
    - ``_client.get_resume_info(task_id)``: devolve o resultado concluído com
      ``last_result_full`` = ``claude_stdout`` (o reconcile parseia/usa).
    """

    def __init__(self, *, claude_stdout: str, claude_rc: int):
        import tempfile
        from pathlib import Path as _P

        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        self._stdout = claude_stdout
        self._rc = claude_rc
        self._ledger = DispatchLedger(
            path=_P(tempfile.mkdtemp(prefix=".test_ledger_")) / "dispatches.json"
        )
        self._seq = 0
        self._client = self  # get_resume_info vive aqui mesmo

    def _ok(self) -> bool:
        return self._rc == 0

    async def review(self, monitor, pr, *, resume: bool = False):
        self._seq += 1
        task_id = f"rev-{self._seq:04d}"
        if resume:
            # Caminho resume bloqueante preservado — devolve outcome estruturado.
            ended = (
                "concluido"
                if ("merged" in self._stdout.lower() and self._ok())
                else "incompleto"
            )
            return WorkOutcome(
                ok=self._ok(),
                text=self._stdout,
                error="" if self._ok() else "boom",
                ended=ended,
                task_id=task_id,
            )
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        if self._ok():
            self._ledger.record(
                DispatchLedger.key_for_pr(pr.number),
                task_id=task_id,
                session_id="",
                stage="pr_review",
            )
        return WorkOutcome(
            ok=self._ok(), text="", error="" if self._ok() else "boom", task_id=task_id
        )

    async def implement(self, monitor, issue, *, resume: bool = False):
        self._seq += 1
        task_id = f"impl-{self._seq:04d}"
        return WorkOutcome(
            ok=self._ok(), text="", error="" if self._ok() else "boom", task_id=task_id
        )

    async def mention(self, monitor, ref, **kwargs):
        return WorkOutcome(
            ok=self._ok(), text=self._stdout, error="" if self._ok() else "boom"
        )

    async def critique(self, monitor, issue):
        return WorkOutcome(
            ok=self._ok(), text=self._stdout, error="" if self._ok() else "boom"
        )

    async def refine(self, monitor, issue):
        return WorkOutcome(
            ok=self._ok(), text=self._stdout, error="" if self._ok() else "boom"
        )

    async def get_resume_info(self, task_id, *, endpoint_url=None):
        return {
            "last_completed_at": 1_700_000_000,
            "last_is_error": not self._ok(),
            "last_result_full": self._stdout,
            "last_result_summary": self._stdout[:200],
            "claude_alive": False,
            "workdir_exists": True,
        }

    def _resolve_endpoint(self, stage):
        return "http://fake-worker:8766"


class TestExtractPrUrl:
    def test_extracts_pr_url(self):
        assert (
            _extract_pr_url("see https://github.com/o/r/pull/9")
            == "https://github.com/o/r/pull/9"
        )

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
            number=1,
            title="t",
            url="u",
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
        # Issue #373: implement is now fire-and-forget. The dispatch claims
        # the issue (revisada → em_implementacao) and returns immediately.
        # Ground truth (PR detection, em_pr transition, notification) is
        # handled by reconcile_implementing_issues on subsequent ticks.
        rev = IssueRef(
            number=2,
            title="impl me",
            url="u",
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
        # Fire-and-forget: implementation_finished only fires on reconcile.
        notifier.implementation_finished.assert_not_called()
        # issues_implemented is incremented in reconcile, not here.
        assert monitor.stats.issues_implemented == 0

    async def test_claims_before_notifying_and_completes_to_em_pr(self):
        # Issue #373: implement is fire-and-forget. The claim (revisada →
        # em_implementacao) still happens, but the em_pr transition is now
        # done by reconcile_implementing_issues on a subsequent tick.
        rev = IssueRef(
            number=2,
            title="impl me",
            url="u",
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
        # The only transition is the atomic claim out of the candidate queue.
        assert calls[0].kwargs == {
            "from_label": WORKFLOW_REVIEWED,
            "to_label": WORKFLOW_IMPLEMENTING,
        }
        # No em_pr transition — that happens in reconcile on next tick.
        for call in calls:
            assert call.kwargs.get("to_label") != WORKFLOW_PR
        notifier.implementation_started.assert_called_once()

    async def test_skips_reviewed_without_batch(self):
        rev = IssueRef(
            number=2,
            title="t",
            url="u",
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
            number=2,
            title="t",
            url="u",
            labels=(WORKFLOW_REVIEWED, WORKFLOW_IMPLEMENTING, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(issues_reviewed=[claimed])
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        notifier.implementation_started.assert_not_called()
        monitor.github.transition_issue.assert_not_called()

    async def test_no_pr_url_parks_without_retry(self):
        # Issue #373: fire-and-forget dispatch — no inline parking.
        # The issue is claimed (revisada → em_implementacao) and dispatched;
        # the reconcile stage checks ground truth. Parking happens via the
        # reaper if the worker never opens a PR.
        rev = IssueRef(
            number=2,
            title="vague meta issue",
            url="u",
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
        # Fire-and-forget: parking is handled by reconcile/reaper, not inline.
        notifier.implementation_parked.assert_not_called()
        notifier.implementation_finished.assert_not_called()
        assert monitor.stats.issues_implemented == 0
        # Only the claim transition fired — never the em_pr completion.
        for call in monitor.github.transition_issue.call_args_list:
            assert call.kwargs.get("to_label") != WORKFLOW_PR

    async def test_claude_failure_parks_issue(self):
        # Issue #373: fire-and-forget dispatch — failure is logged but
        # parking is handled by reconcile/reaper on subsequent ticks.
        rev = IssueRef(
            number=2,
            title="t",
            url="u",
            labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
        )
        monitor, notifier = _make_monitor(
            issues_reviewed=[rev],
            claude_rc=2,
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor.tick()
        # The issue was claimed (started), but fire-and-forget means no
        # immediate parking DM — reconcile/reaper handle it.
        notifier.implementation_started.assert_called_once()
        notifier.implementation_parked.assert_not_called()
        notifier.implementation_finished.assert_not_called()


class TestReconcileImplementingIssues:
    """Issue #373: tests for reconcile_implementing_issues stage.

    Verifies that fire-and-forget dispatched issues are reconciled via
    GitHub ground truth (PR existence) on subsequent ticks."""

    async def test_reconcile_detects_pr_and_transitions_to_em_pr(self):
        """When has_open_pr_for_issue returns True, the issue must transition
        to em_pr, stats.issues_implemented must increment, and
        implementation_finished notification must fire."""
        impl_issue = IssueRef(
            number=99,
            title="working",
            url="u",
            labels=(WORKFLOW_IMPLEMENTING, "~by:default"),
        )
        monitor, notifier = _make_monitor()
        # Mock: list_issues_with_label returns our implementing issue.
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_IMPLEMENTING: [impl_issue],
            }.get(label, []),
        )
        # Ground truth: PR exists!
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        # Keep enable_implement=True so reconcile runs. Disable other
        # stages to reduce noise.
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.reaper_stale_seconds = 0
        await monitor.tick()
        # Must transition to em_pr.
        monitor.github.transition_issue.assert_called_with(
            99,
            from_label=WORKFLOW_IMPLEMENTING,
            to_label=WORKFLOW_PR,
        )
        # Stats must reflect the completion.
        assert monitor.stats.issues_implemented == 1
        # Notification must fire.
        notifier.implementation_finished.assert_called_once_with(99, None)

    async def test_reconcile_no_pr_stays_in_em_implementacao(self):
        """When has_open_pr_for_issue returns False, the issue must stay
        in em_implementacao (no transition, no notification)."""
        impl_issue = IssueRef(
            number=99,
            title="still working",
            url="u",
            labels=(WORKFLOW_IMPLEMENTING, "~by:default"),
        )
        monitor, notifier = _make_monitor()
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_IMPLEMENTING: [impl_issue],
            }.get(label, []),
        )
        # Ground truth: no PR yet.
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=False)
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.reaper_stale_seconds = 0
        await monitor.tick()
        # No transition should happen.
        monitor.github.transition_issue.assert_not_called()
        assert monitor.stats.issues_implemented == 0
        notifier.implementation_finished.assert_not_called()

    async def test_reconcile_skips_blocked_issues(self):
        """Issues with ~workflow:bloqueada must be skipped by reconcile."""
        from deile.orchestration.pipeline.labels import WORKFLOW_BLOCKED

        impl_issue = IssueRef(
            number=99,
            title="blocked",
            url="u",
            labels=(WORKFLOW_IMPLEMENTING, WORKFLOW_BLOCKED, "~by:default"),
        )
        monitor, notifier = _make_monitor()
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_IMPLEMENTING: [impl_issue],
            }.get(label, []),
        )
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.reaper_stale_seconds = 0
        await monitor.tick()
        # Blocked issue must NOT be transitioned.
        monitor.github.transition_issue.assert_not_called()
        assert monitor.stats.issues_implemented == 0

    async def test_reconcile_skips_already_em_pr(self):
        """Issues already in ~workflow:em_pr must be skipped."""
        impl_issue = IssueRef(
            number=99,
            title="done",
            url="u",
            labels=(WORKFLOW_IMPLEMENTING, WORKFLOW_PR, "~by:default"),
        )
        monitor, notifier = _make_monitor()
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_IMPLEMENTING: [impl_issue],
            }.get(label, []),
        )
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.reaper_stale_seconds = 0
        await monitor.tick()
        # Already em_pr — no transition.
        monitor.github.transition_issue.assert_not_called()
        assert monitor.stats.issues_implemented == 0

    async def test_reconcile_skips_non_owned_issues(self):
        """Issues not owned by this monitor must be skipped.

        Default identity (shard_count=1) owns everything via title hash,
        so we use a non-default identity to test ownership filtering."""
        from deile.orchestration.pipeline.identity import MonitorIdentity

        impl_issue = IssueRef(
            number=99,
            title="not mine",
            url="u",
            labels=(WORKFLOW_IMPLEMENTING, "~by:other-monitor"),
        )
        monitor, notifier = _make_monitor()
        # Non-default identity: only owns issues with ~by:monitor-a
        monitor.identity = MonitorIdentity(monitor_id="monitor-a")
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_IMPLEMENTING: [impl_issue],
            }.get(label, []),
        )
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.reaper_stale_seconds = 0
        await monitor.tick()
        # Not our issue — no transition.
        monitor.github.transition_issue.assert_not_called()

    async def test_reconcile_runs_before_implement_in_tick(self):
        """When reconcile completes an issue, the freed slot should be
        available for new claims in the SAME tick. This test verifies
        that reconcile runs and can consume in-flight issues before
        implement claims new ones."""
        # An issue in em_implementacao with a PR ready to be detected.
        impl_issue = IssueRef(
            number=42,
            title="completed",
            url="u",
            labels=(WORKFLOW_IMPLEMENTING, "~by:default"),
        )
        monitor, notifier = _make_monitor()
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_IMPLEMENTING: [impl_issue],
                WORKFLOW_REVIEWED: [],
            }.get(label, []),
        )
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.reaper_stale_seconds = 0
        # enable_implement is True (default) — reconcile runs before it.
        await monitor.tick()
        # Reconcile must have transitioned the issue to em_pr.
        monitor.github.transition_issue.assert_called_with(
            42,
            from_label=WORKFLOW_IMPLEMENTING,
            to_label=WORKFLOW_PR,
        )
        assert monitor.stats.issues_implemented == 1
        notifier.implementation_finished.assert_called_once_with(42, None)


def _setup_pr_merge_groundtruth(
    monitor,
    pr_number: int,
    *,
    head_ref: str = "auto/issue-2",
    title: str = "prt",
    url: str = "https://x/pull/10",
) -> None:
    """Após o dispatch fresh (tick 1), o reconcile do tick 2 detecta MERGE por
    ground-truth: ``get_pr(n)`` devolve None (PR não mais aberta). A PR segue
    listada em ``em_andamento`` (com ledger entry) pro reconcile pegá-la."""
    in_progress = PrRef(
        number=pr_number,
        title=title,
        url=url,
        labels=(REVIEW_IN_PROGRESS,),
        head_ref=head_ref,
    )
    monitor.github.list_open_prs = AsyncMock(return_value=[in_progress])
    monitor.github.get_pr = AsyncMock(return_value=None)  # merged → não-aberta


async def _review_to_merge(
    monitor, pr_number: int, *, head_ref: str = "auto/issue-2"
) -> None:
    """Dirige o fluxo review fresh → reconcile-merge em dois ticks."""
    await monitor.tick()
    _setup_pr_merge_groundtruth(monitor, pr_number, head_ref=head_ref)
    await monitor.tick()


class TestStage3PrReview:
    async def test_picks_up_unclaimed_open_pr(self):
        # Fire-and-forget (issue #373): tick 1 despacha fresh (pr_picked_up);
        # tick 2 reconcilia por ground-truth (PR merged) → pr_reviewed.
        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-2",
        )
        monitor, notifier = _make_monitor(prs=[pr], claude_stdout="merged.")
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_called_once()
        notifier.pr_reviewed.assert_not_called()  # ainda não — reconcile no tick 2
        _setup_pr_merge_groundtruth(monitor, 10)
        await monitor.tick()
        notifier.pr_reviewed.assert_called_once()
        assert monitor.stats.prs_reviewed == 1

    async def test_skips_drafts(self):
        pr = PrRef(
            number=10, title="t", url="u", labels=(), head_ref="x", is_draft=True
        )
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_not_called()

    async def test_skips_concluded_prs(self):
        pr = PrRef(
            number=10, title="t", url="u", labels=(REVIEW_CONCLUDED,), head_ref="x"
        )
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()
        notifier.pr_picked_up.assert_not_called()

    async def test_skips_in_progress_prs(self):
        pr = PrRef(
            number=10, title="t", url="u", labels=(REVIEW_IN_PROGRESS,), head_ref="x"
        )
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
    from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor
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
    for attr in (
        "issue_picked_up",
        "issue_reviewed",
        "implementation_started",
        "implementation_finished",
        "pr_picked_up",
        "pr_reviewed",
        "issue_auto_classified",
        "follow_ups_processed",
        "error",
        "pr_auto_classified",
        "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    schedule_store = MagicMock()
    schedule_store.load = MagicMock(
        return_value=MagicMock(
            recurring=[], oneshot=[], compute_pending=MagicMock(return_value=[])
        )
    )

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
            number=77,
            title="my pr",
            url="https://x/pull/77",
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-5",
        )
        monitor, notifier = _make_monitor(prs=[pr])
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await monitor.tick()

        # ownership label must have been added
        ownership = monitor.identity.ownership_label()
        add_labels_calls = monitor.github.add_labels.call_args_list
        ownership_calls = [
            c for c in add_labels_calls if ownership in (c.args[2] if c.args else [])
        ]
        assert (
            ownership_calls
        ), f"expected add_labels call with {ownership!r}; calls were: {add_labels_calls}"


# ---------------------------------------------------------------------------
# Stage 4: follow-up issue creation
# ---------------------------------------------------------------------------


class TestStage4FollowUps:
    def _merged_pr(self) -> PrRef:
        return PrRef(
            number=55,
            title="fix: parser",
            url="https://github.com/o/r/pull/55",
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-10",
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
        monitor, _ = _make_monitor(
            prs=[pr], claude_stdout="PR merged successfully", claude_rc=0
        )
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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
        await _review_to_merge(monitor, 55, head_ref="auto/issue-10")
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


def test_publish_status_state_integrates_with_real_state(monkeypatch):
    """Regression: ``_publish_status_state`` must succeed against the real
    ``PipelineStatusState`` API (issue #347).

    The first ``monitor → status server`` wiring landed (PR #352, commit
    26e139d) with a kwargs mismatch — every publish raised TypeError under
    the outer ``except Exception``, so ``/v1/pipeline-status`` reported
    zeros forever.  This test exercises the integration end-to-end and
    asserts the snapshot reflects what was published.
    """
    import importlib.util
    import sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[4]
    server_path = repo_root / "infra" / "k8s" / "pipeline_status_server.py"
    spec = importlib.util.spec_from_file_location(
        "pipeline_status_server_pub_test",
        str(server_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_status_server_pub_test"] = mod
    spec.loader.exec_module(mod)

    monitor, _ = _make_monitor()
    state = mod.PipelineStatusState()
    monitor._status_state = state
    # Simulate one tick's worth of stats then publish.
    monitor._stats.ticks = 4
    monitor._stats.errors = 1
    import time as _time

    monitor._publish_status_state(state, _time.monotonic() - 0.5)

    snap = state.snapshot_status()
    assert snap["ticks_total"] == 4
    assert snap["errors_total"] == 1
    assert snap["last_tick_at"] is not None
    assert snap["last_tick_duration_seconds"] is not None


# ---------------------------------------------------------------------------
# Tick-summary INFO log (issue #349)
# ---------------------------------------------------------------------------

import logging  # noqa: E402


class TestTickSummary:
    """Issue #349: verify the INFO-level tick-summary log fires at the end of
    every tick with correct per-tick deltas."""

    async def test_idle_tick_logs_summary_with_zeros(self, caplog):
        """A tick with no work must still emit a summary (all-zero counters)."""
        monitor, _ = _make_monitor()
        monitor.config.enable_classify = False
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1, f"expected 1 tick-summary record, got {len(records)}"
        msg = records[0].message
        assert "tick #1 done in" in msg
        assert "classified=0 reviewed=0 implemented=0 dispatched=0" in msg
        assert "backlog=" in msg

    async def test_tick_summary_reflects_classify_delta(self, caplog):
        """Classifying one issue must show classified=1 in the summary."""
        new_issue = IssueRef(number=1, title="t", url="u", labels=("bug",))
        monitor, _ = _make_monitor()
        monitor.config.enable_classify = True
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        monitor.github.list_unclassified_issues = AsyncMock(
            return_value=[new_issue],
        )
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1
        msg = records[0].message
        assert "classified=1" in msg, f"expected classified=1, got: {msg}"

    async def test_tick_summary_reflects_review_delta(self, caplog):
        """Reviewing one issue must show reviewed=1 in the summary."""
        new_issue = IssueRef(number=1, title="t", url="u", labels=(WORKFLOW_NEW,))
        monitor, _ = _make_monitor(issues_new=[new_issue])
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1
        msg = records[0].message
        assert "reviewed=1" in msg, f"expected reviewed=1, got: {msg}"

    async def test_tick_summary_reflects_implement_delta(self, caplog):
        # Issue #373: fire-and-forget dispatch — issues_implemented is only
        # incremented by reconcile_implementing_issues on subsequent ticks.
        # A fresh dispatch increments dispatched but not implemented.
        rev = IssueRef(
            number=2,
            title="impl me",
            url="u",
            labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
        )
        monitor, _ = _make_monitor(
            issues_reviewed=[rev],
            claude_stdout="Done. https://github.com/owner/name/pull/3",
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1
        msg = records[0].message
        # Fire-and-forget: implemented counter stays 0 until reconcile.
        # dispatched counter should show the claim.
        assert "implemented=0" in msg, f"expected implemented=0, got: {msg}"

    async def test_tick_summary_reflects_dispatched_delta(self, caplog):
        """Concluir uma review (merge detectado no reconcile) deve mostrar
        dispatched=1 no resumo. Fire-and-forget (issue #373): o contador
        ``prs_reviewed`` só sobe no reconcile (tick 2), não no dispatch fresh."""
        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-2",
        )
        monitor, _ = _make_monitor(prs=[pr], claude_stdout="merged.")
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        # Tick 1: dispatch fresh fire-and-forget (dispatched=0 ainda).
        await monitor.tick()
        _setup_pr_merge_groundtruth(monitor, 10)
        # Tick 2: reconcile detecta merge → prs_reviewed++ → dispatched=1.
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1
        msg = records[0].message
        assert "dispatched=1" in msg, f"expected dispatched=1, got: {msg}"

    async def test_tick_summary_backlog_unavailable_on_forge_error(self, caplog):
        """When forge.list_issues_with_label raises, the summary must show
        backlog=unavailable instead of crashing the tick."""
        monitor, _ = _make_monitor()
        # Disable ALL stages so only the tick-summary log's own forge calls fail.
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.enable_refinement_gate = False
        monitor.config.enable_resume = False
        monitor.config.reaper_stale_seconds = 0
        # Make the forge's backlog query itself raise.
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=Exception("gh api down"),
        )
        monitor.github.list_open_prs = AsyncMock(
            side_effect=Exception("gh api down"),
        )
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1
        msg = records[0].message
        assert (
            "backlog=unavailable" in msg
        ), f"expected backlog=unavailable when forge fails, got: {msg}"

    async def test_tick_summary_includes_backlog_counts(self, caplog):
        """When forge responds, the summary must include backlog issue/PR counts."""
        monitor, _ = _make_monitor()
        # Disable all stages so only the tick-summary calls the forge for backlog.
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        monitor.config.enable_mention_handling = False
        monitor.config.enable_refinement_gate = False
        monitor.config.enable_resume = False
        monitor.config.reaper_stale_seconds = 0
        # Override with a forge that returns known backlog counts.
        monitor.forge.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: {
                WORKFLOW_NEW: [
                    IssueRef(number=1, title="a", url="u", labels=(WORKFLOW_NEW,))
                ],
                WORKFLOW_REVIEWED: [],
                WORKFLOW_IMPLEMENTING: [
                    IssueRef(
                        number=2, title="b", url="u", labels=(WORKFLOW_IMPLEMENTING,)
                    ),
                    IssueRef(
                        number=3, title="c", url="u", labels=(WORKFLOW_IMPLEMENTING,)
                    ),
                ],
            }.get(label, []),
        )
        monitor.forge.list_open_prs = AsyncMock(
            return_value=[
                PrRef(number=10, title="p", url="u", labels=(), head_ref="x"),
            ],
        )
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1
        msg = records[0].message
        assert (
            "backlog={issues:3 prs:1}" in msg
        ), f"expected backlog={{issues:3 prs:1}}, got: {msg}"

    async def test_tick_summary_records_count_isolated_from_external_handlers(
        self, caplog
    ):
        """Tick-summary INFO records are captured regardless of prior logging.disable.

        Issue #432: deile/cli.py calls logging.disable(CRITICAL) during CLI runs
        (TestFlagSmoke → cli_main). Without _clean_logging_handlers restoring
        logging.root.manager.disable to 0 between tests, all 7 TestTickSummary
        tests fail when run after TestFlagSmoke because INFO-level records are
        silently dropped.
        """
        assert logging.root.manager.disable == 0, (
            f"logging.root.manager.disable should be 0 at test start; "
            f"got {logging.root.manager.disable}. "
            "_clean_logging_handlers may have failed to restore it."
        )
        monitor, _ = _make_monitor()
        with caplog.at_level(
            logging.INFO, logger="deile.orchestration.pipeline.monitor"
        ):
            await monitor.tick()
        records = [r for r in caplog.records if "tick #" in r.message]
        assert len(records) == 1, (
            f"expected exactly 1 tick-summary record; got {len(records)}. "
            "Logging isolation may be broken (manager.disable not restored)."
        )
