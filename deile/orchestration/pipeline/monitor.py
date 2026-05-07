"""1-minute polling loop that drives the autonomous pipeline.

The :class:`PipelineMonitor` ticks every ``poll_interval_seconds`` (default 60s)
and, on each tick:

1. Checks for issues with label ``~workflow:nova`` *and no ~batch:* — claims
   the next one, transitions to ``~workflow:em_revisao``, asks DEILE to revise
   the body, then transitions to ``~workflow:revisada``.
2. Checks for issues with ``~workflow:revisada`` and no ``~workflow:em_pr`` —
   claims, sets up a worktree, invokes Claude Code one-shot to implement, and
   on success transitions the issue to ``~workflow:em_pr``.
3. Checks for open PRs without ``~review:concluida`` — claims, invokes Claude
   Code one-shot to review/correct/merge, then marks ``~review:concluida``.

Discord notifications (DiscordNotifier) fire at every transition.

This monitor is single-instance by design: locking via ``~batch:`` labels is a
best-effort coordination mechanism, not a true distributed lock.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from deile.orchestration.pipeline.claude_dispatcher import (
    ClaudeDispatcher, render_implement_prompt, render_review_prompt)
from deile.orchestration.pipeline.constants import (
    PIPELINE_MSG_TRUNCATE_CHARS, PIPELINE_POLL_INTERVAL_SECONDS,
    PIPELINE_STOP_TIMEOUT_SECONDS)
from deile.orchestration.pipeline.github_client import (GhCommandError,
                                                        GitHubClient, IssueRef)
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.labels import (REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING, WORKFLOW_NEW,
                                                 WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)
from deile.orchestration.pipeline.lockfile import LockHeldError
from deile.orchestration.pipeline.lockfile import acquire as acquire_lock
from deile.orchestration.pipeline.lockfile import release as release_lock
from deile.orchestration.pipeline.notifier import DiscordNotifier
from deile.orchestration.pipeline.scheduler import PendingRun, ScheduleStore
from deile.orchestration.pipeline.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+", re.IGNORECASE)


@dataclass
class PipelineConfig:
    repo: str
    base_repo_path: Path
    poll_interval_seconds: int = PIPELINE_POLL_INTERVAL_SECONDS
    main_branch: str = "main"
    # ``branch_prefix`` is the *legacy* per-instance default. When an
    # ``identity`` is provided to :class:`PipelineMonitor`, the actual prefix
    # is derived from ``identity.branch_prefix("auto") + "/issue-"`` so two
    # monitors don't collide on branch names. ``branch_prefix`` here remains
    # the fallback for single-monitor (default identity) deployments.
    branch_prefix: str = "auto/issue-"
    notify_user_id: Optional[str] = None
    enable_review: bool = True
    enable_implement: bool = True
    enable_pr_review: bool = True
    enable_classify: bool = True
    classifiable_labels: Tuple[str, ...] = ("intent", "bug", "refactor", "feature_request")
    classify_skip_labels: Tuple[str, ...] = ("infra",)
    # When True, the monitor acquires a PID lockfile under base_repo_path
    # named after the identity. Two monitors with the same monitor_id on
    # the same host will fail to start. Default off because most tests
    # don't need it; production callers should set True.
    use_pid_lock: bool = False


@dataclass
class _Stats:
    ticks: int = 0
    issues_reviewed: int = 0
    issues_implemented: int = 0
    prs_reviewed: int = 0
    errors: int = 0
    catchup_runs: int = 0
    scheduled_runs: int = 0


class PipelineMonitor:
    """Async polling driver of the issue → PR → merge pipeline."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        github: Optional[GitHubClient] = None,
        worktrees: Optional[WorktreeManager] = None,
        claude: Optional[ClaudeDispatcher] = None,
        notifier: Optional[DiscordNotifier] = None,
        review_callback: Optional[Callable[[IssueRef], Awaitable[str]]] = None,
        identity: Optional[MonitorIdentity] = None,
        schedule_store: Optional[ScheduleStore] = None,
    ) -> None:
        self.config = config
        self.identity = identity or MonitorIdentity.from_env()
        self.github = github or GitHubClient(config.repo)
        self.worktrees = worktrees or WorktreeManager(
            config.base_repo_path,
            main_branch=config.main_branch,
            subdir=self.identity.worktree_subdir(),
        )
        self.claude = claude or ClaudeDispatcher()
        self.notifier = notifier or DiscordNotifier(config.notify_user_id)
        self._review_cb = review_callback
        self._stats = _Stats()
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._held_lock: Optional[Path] = None
        # Schedule store — when present, schedule entries drive when each
        # action fires (instead of the fixed poll interval). On startup the
        # monitor first drains any catch-up queue (entries whose run time
        # has already passed), then enters the polling loop where every
        # tick re-checks for due entries. If no schedule file exists, the
        # monitor falls back to legacy "every action every poll" behaviour.
        self.schedule_store = schedule_store or ScheduleStore(
            config.base_repo_path, monitor_id=self.identity.monitor_id
        )

    # ------------------------------------------------------------------
    # identity-aware naming helpers
    # ------------------------------------------------------------------

    def branch_for_issue(self, issue_number: int) -> str:
        """Per-monitor branch name for stage 2 implementation."""
        if self.identity.is_default:
            return f"{self.config.branch_prefix}{issue_number}"
        # Per-monitor prefix overrides the legacy config.branch_prefix.
        return f"{self.identity.branch_prefix('auto')}/issue-{issue_number}"

    def _owns_pr_branch(self, head_ref: str) -> bool:
        """Return True if the PR's branch was opened by THIS monitor.

        Used to scope stage 3 to PRs the local monitor implemented. Default
        identity owns any branch starting with ``auto/issue-`` (legacy path).
        """
        if not head_ref:
            return False
        if self.identity.is_default:
            # Legacy: claim PRs whose branch matches the legacy prefix and has
            # no monitor segment.
            return head_ref.startswith("auto/issue-")
        return head_ref.startswith(f"{self.identity.branch_prefix('auto')}/")

    @property
    def stats(self) -> _Stats:
        return self._stats

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling loop.

        PID locking is enabled when:
        - ``config.use_pid_lock`` is explicitly set, **or**
        - the identity is non-default (any named monitor, any sharded deployment).

        The second condition is intentional: a non-default identity implies a
        multi-monitor deployment where two instances with the same ``monitor_id``
        on the same host are a guaranteed-bug — they would race on the same
        worktree sub-directory and schedule file. The lockfile is the last line
        of defence against operator error.
        """
        if self.is_running:
            return
        should_lock = self.config.use_pid_lock or not self.identity.is_default
        if should_lock:
            lock_path = Path(self.config.base_repo_path) / self.identity.lockfile_name()
            try:
                self._held_lock = acquire_lock(lock_path)
            except LockHeldError as exc:
                logger.error(
                    "another monitor with id=%s is already running (PID %d); refusing start",
                    self.identity.monitor_id, exc.holder_pid,
                )
                raise
        await self.github.ensure_pipeline_labels()
        await self._catch_up_pending()
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(), name=f"pipeline-monitor-{self.identity.monitor_id}"
        )

    async def _catch_up_pending(self) -> None:
        """On startup, drain any schedule entries whose time already passed."""
        try:
            schedule = self.schedule_store.load()
        except Exception as exc:  # noqa: BLE001 — schedule errors must not block boot
            logger.warning("schedule load failed; skipping catch-up: %s", exc)
            return
        pending = schedule.compute_pending()
        if not pending:
            return
        logger.info(
            "monitor %s catching up on %d missed runs",
            self.identity.monitor_id, len(pending),
        )
        for run in pending:
            await self._run_scheduled(run)
            schedule.mark_run(run)
            self._stats.catchup_runs += 1
        try:
            self.schedule_store.save(schedule)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist schedule after catch-up: %s", exc)

    async def _run_scheduled(self, run: PendingRun) -> None:
        """Execute a single scheduled action by name."""
        if run.action == "classify" and self.config.enable_classify:
            await self._classify_new_issues()
        elif run.action == "review" and self.config.enable_review:
            await self._review_one_new_issue()
        elif run.action == "implement" and self.config.enable_implement:
            await self._implement_one_reviewed_issue()
        elif run.action == "pr_review" and self.config.enable_pr_review:
            await self._review_one_open_pr()
        else:
            logger.debug("scheduled action %s skipped (disabled or unknown)", run.action)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=PIPELINE_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._held_lock is not None:
            release_lock(self._held_lock)
            self._held_lock = None

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                self._stats.errors += 1
                logger.exception("pipeline tick crashed: %s", exc)
                await self.notifier.error("monitor.tick", str(exc))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _this_monitor_owns(self, issue: IssueRef) -> bool:
        """Return True if this monitor should process the given issue."""
        if self.identity.is_default:
            return self.identity.owns(issue.title)
        return self.identity.ownership_label() in issue.labels

    # ------------------------------------------------------------------
    # one tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        self._stats.ticks += 1
        logger.debug("pipeline tick #%d", self._stats.ticks)

        # When a schedule file exists with at least one entry, the schedule
        # is authoritative: each tick runs only the actions whose cron
        # window opened since the previous tick. Without a schedule, fall
        # back to legacy "every action every tick" behaviour.
        try:
            schedule = self.schedule_store.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("schedule load failed on tick; falling back to legacy mode: %s", exc)
            schedule = None
        if schedule and (schedule.recurring or schedule.oneshot):
            pending = schedule.compute_pending()
            for run in pending:
                await self._run_scheduled(run)
                schedule.mark_run(run)
                self._stats.scheduled_runs += 1
            if pending:
                try:
                    self.schedule_store.save(schedule)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not persist schedule after tick: %s", exc)
            return

        if self.config.enable_classify:
            await self._classify_new_issues()
        if self.config.enable_review:
            await self._review_one_new_issue()
        if self.config.enable_implement:
            await self._implement_one_reviewed_issue()
        if self.config.enable_pr_review:
            await self._review_one_open_pr()

    # ----- stage 0: auto-classify new issues -----------------------

    async def _classify_new_issues(self) -> None:
        """Apply ``~workflow:nova`` to open issues that are eligible but unclassified.

        An issue is eligible when:
        - it has at least one label in ``config.classifiable_labels``
        - it has no label in ``config.classify_skip_labels``
        - it has no pipeline labels (nothing starting with ``~``)
        - its body is non-empty (filled template)
        - it falls in this monitor's shard
        """
        try:
            issues = await self.github.list_unclassified_issues()
        except GhCommandError as exc:
            logger.warning("could not list unclassified issues: %s", exc)
            return

        for issue in issues:
            if not any(lb in self.config.classifiable_labels for lb in issue.labels):
                continue
            if any(lb in self.config.classify_skip_labels for lb in issue.labels):
                continue
            if not self.identity.owns(issue.title):
                continue
            if not issue.body.strip():
                logger.debug("issue #%s has empty body; skipping auto-classification", issue.number)
                continue
            try:
                await self.github.add_labels("issue", issue.number, [WORKFLOW_NEW])
                await self.github.comment_on_issue(
                    issue.number,
                    f"🤖 **DEILE auto-classificação** — esta issue foi adicionada à fila do pipeline "
                    f"autônomo (`{WORKFLOW_NEW}`).\n\n"
                    f"Para excluir da fila, remova o label `{WORKFLOW_NEW}`.",
                )
                await self.notifier.issue_auto_classified(issue.number, issue.title, issue.url)
                logger.info("auto-classified issue #%s as %s", issue.number, WORKFLOW_NEW)
            except Exception as exc:  # noqa: BLE001 — best-effort, never abort loop
                logger.warning("auto-classification of #%s failed: %s", issue.number, exc)
                await self.notifier.error(
                    f"auto-classify #{issue.number}", f"{type(exc).__name__}: {exc}"
                )

    # ----- stage 1: review ------------------------------------------

    async def _review_one_new_issue(self) -> None:
        try:
            issues = await self.github.list_issues_with_label(WORKFLOW_NEW, limit=50)
        except GhCommandError as exc:
            logger.warning("could not list new issues: %s", exc)
            return
        # Shard filter: only consider issues whose hash falls in our shard.
        target = next(
            (i for i in issues if i.batch_id is None and self.identity.owns(i.title)),
            None,
        )
        if target is None:
            return
        batch = await self.github.claim_with_batch("issue", target.number, target.title)
        if batch is None:
            return
        # Tag ownership so other monitors can identify who claimed this.
        await self.github.add_labels("issue", target.number, [self.identity.ownership_label()])
        await self.notifier.issue_picked_up(target.number, target.title, target.url)
        try:
            await self.github.transition_issue(
                target.number, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
            )
            if self._review_cb is not None:
                comment = await self._review_cb(target)
                if comment:
                    await self.github.comment_on_issue(target.number, comment)
            await self.github.transition_issue(
                target.number, from_label=WORKFLOW_REVIEWING, to_label=WORKFLOW_REVIEWED
            )
        except Exception as exc:  # noqa: BLE001 — surface and continue
            logger.exception("review of #%s failed", target.number)
            await self.notifier.error(
                f"review issue #{target.number}", f"{type(exc).__name__}: {exc}"
            )
            return
        self._stats.issues_reviewed += 1
        await self.notifier.issue_reviewed(target.number, target.title, target.url)

    # ----- stage 2: implement ---------------------------------------

    async def _implement_one_reviewed_issue(self) -> None:
        try:
            issues = await self.github.list_issues_with_label(WORKFLOW_REVIEWED, limit=50)
        except GhCommandError as exc:
            logger.warning("could not list reviewed issues: %s", exc)
            return
        # Stage 2 only picks up issues this monitor claimed in stage 1.
        target = next(
            (
                i for i in issues
                if WORKFLOW_PR not in i.labels
                and i.batch_id is not None
                and self._this_monitor_owns(i)
            ),
            None,
        )
        if target is None:
            return
        branch = self.branch_for_issue(target.number)
        await self.notifier.implementation_started(target.number, target.title, branch)
        try:
            worktree = await self.worktrees.create_branch_worktree(branch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("worktree setup for #%s failed", target.number)
            await self.notifier.error(
                f"worktree #{target.number}", f"{type(exc).__name__}: {exc}"
            )
            return
        prompt = render_implement_prompt(self.config.repo, target.number, target.title, target.body)
        result = await self.claude.run(prompt, cwd=worktree.path)
        pr_url = _extract_pr_url(result.stdout)
        if not result.ok:
            await self.notifier.error(
                f"implement #{target.number}", result.stderr.strip()[:PIPELINE_MSG_TRUNCATE_CHARS] or "non-zero exit"
            )
            return
        try:
            await self.github.transition_issue(
                target.number, from_label=WORKFLOW_REVIEWED, to_label=WORKFLOW_PR
            )
        except GhCommandError as exc:
            logger.warning("could not transition issue #%s to em_pr: %s", target.number, exc)
        self._stats.issues_implemented += 1
        await self.notifier.implementation_finished(target.number, pr_url)

    # ----- stage 3: review PR ---------------------------------------

    async def _review_one_open_pr(self) -> None:
        try:
            prs = await self.github.list_open_prs(limit=50)
        except GhCommandError as exc:
            logger.warning("could not list PRs: %s", exc)
            return
        # Scope stage 3 to PRs whose head branch belongs to THIS monitor
        # (so we never review a peer's PR). Default-identity monitors keep
        # the legacy behaviour: any PR with a matching head_ref or none.
        target = next(
            (
                pr
                for pr in prs
                if not pr.is_draft
                and REVIEW_CONCLUDED not in pr.labels
                and REVIEW_IN_PROGRESS not in pr.labels
                and pr.batch_id is None
                and self._owns_pr_branch(pr.head_ref)
            ),
            None,
        )
        if target is None:
            return
        batch = await self.github.claim_with_batch("pr", target.number, target.title)
        if batch is None:
            return
        # Tag ownership so other monitors can identify who claimed this PR —
        # mirrors the identical pattern in stage 1 for issues.
        await self.github.add_labels("pr", target.number, [self.identity.ownership_label()])
        await self.notifier.pr_picked_up(target.number, target.title, target.url)
        try:
            await self.github.transition_pr(
                target.number, from_label=REVIEW_PENDING, to_label=REVIEW_IN_PROGRESS
            )
        except GhCommandError:
            # ~review:pendente may not be set; that's ok.
            await self.github.add_labels("pr", target.number, [REVIEW_IN_PROGRESS])
        # The PR was opened on a branch — for the worktree, we just need a
        # checkout of that branch. Use the same naming convention if the branch
        # follows it; otherwise fall back to ``main`` and let Claude `gh pr
        # checkout`.
        worktree_branch = target.head_ref or f"pr/{target.number}"
        try:
            wt = await self.worktrees.create_branch_worktree(worktree_branch)
        except Exception as exc:  # noqa: BLE001
            await self.notifier.error(
                f"PR worktree #{target.number}", f"{type(exc).__name__}: {exc}"
            )
            return
        prompt = render_review_prompt(self.config.repo, target.number, target.title)
        result = await self.claude.run(prompt, cwd=wt.path)
        merged = result.ok and "merged" in result.stdout.lower()
        try:
            await self.github.transition_pr(
                target.number, from_label=REVIEW_IN_PROGRESS, to_label=REVIEW_CONCLUDED
            )
        except GhCommandError as exc:
            logger.warning("could not transition PR #%s to concluida: %s", target.number, exc)
        self._stats.prs_reviewed += 1
        await self.notifier.pr_reviewed(target.number, target.title, target.url, merged=merged)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_pr_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = _PR_URL_RE.search(text)
    return m.group(0) if m else None
