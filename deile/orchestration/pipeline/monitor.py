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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from deile.orchestration.pipeline import stages
from deile.orchestration.pipeline.actions import ACTIONS_BY_NAME
from deile.orchestration.pipeline.claude_dispatcher import ClaudeDispatcher
from deile.orchestration.pipeline.constants import (
    PIPELINE_POLL_INTERVAL_SECONDS, PIPELINE_STOP_TIMEOUT_SECONDS)
from deile.orchestration.pipeline.github_client import GitHubClient, IssueRef
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.implementer import (PipelineImplementer,
                                                      build_implementer)
from deile.orchestration.pipeline.lockfile import LockHeldError
from deile.orchestration.pipeline.lockfile import acquire as acquire_lock
from deile.orchestration.pipeline.lockfile import release as release_lock
from deile.orchestration.pipeline.notifier import DiscordNotifier
from deile.orchestration.pipeline.scheduler import PendingRun, ScheduleStore
from deile.orchestration.pipeline.stages import (_extract_pr_url,
                                                 _render_follow_up_report)
from deile.orchestration.pipeline.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

# Re-exported from ``stages`` for backwards compatibility: existing tests and
# callers import ``_extract_pr_url``/``_render_follow_up_report`` from this
# module. The canonical definitions now live in ``stages.py``.
__all__ = ["PipelineConfig", "PipelineMonitor", "_extract_pr_url",
           "_render_follow_up_report"]


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
    enable_follow_ups: bool = True
    # Labels que disparam a auto-classificação. Convenção do projeto: o label
    # CANÔNICO é IDÊNTICO ao prefixo entre colchetes do título da issue
    # (``[FEATURE]`` → ``feature``, ``[BUG]`` → ``bug``, ``[INTENT]`` →
    # ``intent``, ``[REFACTOR]`` → ``refactor``), espelhando o ``labels:`` dos
    # templates em ``.github/ISSUE_TEMPLATE/``. ``security`` é aceito sem
    # template dedicado. ``enhancement`` entra como alias tolerado: é o label
    # padrão do GitHub para features e o que o template antigo aplicava — sem
    # ele, uma issue de feature criada com o label convencional ficava
    # invisível ao pipeline (foi exatamente o que travou a demo: a #247 nasceu
    # com ``enhancement`` e só andou quando virou ``intent``).
    classifiable_labels: frozenset = frozenset(
        {"intent", "bug", "refactor", "feature", "enhancement", "security"}
    )
    classify_skip_labels: frozenset = frozenset({"infra"})
    enable_pr_triage: bool = True
    enable_mention_handling: bool = True
    mention_handle: str = "@deile-one"
    # Default True: two simultaneous /pipeline start on the same host fail fast.
    use_pid_lock: bool = True
    # When True, Stage 3 reviews any non-draft PR regardless of head branch origin.
    enable_review_human_prs: bool = False
    # Limit catch-up to runs missed within the last N hours.  None = no limit (legacy).
    # Recommended: 1–2 hours so a long outage does not flood the queue.
    bootstrap_replay_window_hours: Optional[int] = 1
    # When True, cleanup_merged_branches() runs once on startup.
    enable_worktree_cleanup: bool = True
    # Which strategy implements/reviews the work (see ``implementer.py``):
    #   "claude"       → run ``claude -p`` in a local git worktree (legacy);
    #   "deile_worker" → dispatch to the deile-worker Pod over HTTP (DEILE-to-DEILE).
    # The dataclass default stays "claude" so hand-built configs (unit tests
    # that inject a mocked ``claude``) keep the legacy behaviour. The *product*
    # default is "deile_worker", resolved from settings in
    # ``build_default_pipeline_config`` — every real entry point (CLI autostart,
    # /pipeline tool/command, the deile-pipeline deployment) uses the worker.
    dispatch_mode: str = "claude"


def build_default_pipeline_config(*, use_pid_lock: bool = True) -> PipelineConfig:
    """Construct a :class:`PipelineConfig` from the default repo/path/settings.

    Centralizes the repo + base-path + notify-user resolution shared by the
    ``pipeline`` tool and the ``/pipeline`` slash command, so the two
    surfaces cannot drift on how a default config is assembled. ``use_pid_lock``
    lets the ``/pipeline start --no-pid-lock`` flag route through this helper
    too instead of hand-building its own config.
    """
    from deile.config.settings import get_settings
    from deile.orchestration.pipeline.constants import resolve_pipeline_repo
    from deile.tools._pipeline_paths import resolve_base_path

    settings = get_settings()
    dispatch_mode = (settings.pipeline_dispatch_mode or "deile_worker").strip().lower()
    return PipelineConfig(
        repo=resolve_pipeline_repo(),
        base_repo_path=resolve_base_path(),
        notify_user_id=settings.pipeline_notify_user_id,
        use_pid_lock=use_pid_lock,
        dispatch_mode=dispatch_mode,
        # The deile_worker path implements/reviews inside the worker Pod; the
        # pipeline process has no local clone, so the on-startup worktree
        # cleanup would only emit warnings. Keep it for the claude path.
        enable_worktree_cleanup=dispatch_mode in ("claude", "claude_code", "claude-code"),
    )


@dataclass
class _Stats:
    ticks: int = 0
    issues_reviewed: int = 0
    issues_implemented: int = 0
    prs_reviewed: int = 0
    issues_classified: int = 0
    errors: int = 0
    # Separate counters allow operators to distinguish gh CLI failures from Claude failures.
    gh_errors: int = 0
    claude_errors: int = 0
    catchup_runs: int = 0
    scheduled_runs: int = 0
    follow_ups_opened: int = 0
    follow_ups_skipped: int = 0
    # Incremented when a scheduled action is disabled via enable_* config.
    skipped_runs: int = 0
    prs_classified: int = 0
    mentions_processed: int = 0


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
        post_merge_callback: Optional[Callable[[int, str, str], Awaitable[None]]] = None,
        identity: Optional[MonitorIdentity] = None,
        schedule_store: Optional[ScheduleStore] = None,
        implementer: Optional[PipelineImplementer] = None,
    ) -> None:
        self.config = config
        self.identity = identity or MonitorIdentity.from_env()
        self.github = github or GitHubClient(config.repo)
        # WorktreeManager validates that base_repo_path is a git repo at
        # construction. The deile_worker strategy never creates local
        # worktrees (the worker Pod owns its own clone) and runs where
        # base_repo_path is typically NOT a git repo — so only build the
        # manager for the claude strategy (or when one is injected).
        if worktrees is not None:
            self.worktrees = worktrees
        elif (config.dispatch_mode or "claude").strip().lower() in (
            "claude", "claude_code", "claude-code"
        ):
            self.worktrees = WorktreeManager(
                config.base_repo_path,
                main_branch=config.main_branch,
                subdir=self.identity.worktree_subdir(),
            )
        else:
            self.worktrees = None
        self.claude = claude or ClaudeDispatcher()
        # Strategy that does the implement/review/mention work. When not
        # injected it is selected from ``config.dispatch_mode``: "claude"
        # uses ``self.claude`` + ``self.worktrees``; "deile_worker" dispatches
        # to the worker Pod over HTTP. Stage handlers delegate to it.
        self.implementer = implementer or build_implementer(config.dispatch_mode)
        self.notifier = notifier or DiscordNotifier(config.notify_user_id)
        self._review_cb = review_callback
        self._stats = _Stats()
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._held_lock: Optional[Path] = None
        self._post_merge_cb = post_merge_callback
        self._mention_cursor_path = Path(config.base_repo_path) / "data" / "mention_cursor.txt"
        self._mention_cursor: Optional[datetime] = None
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

    def _owns_pr_branch(self, head_ref: str, *, pr_number: int = 0) -> bool:
        """Return True if the PR's branch was opened by THIS monitor.

        Used to scope stage 3 to PRs the local monitor implemented. Default
        identity owns any branch starting with ``auto/issue-`` (legacy path).

        When ``config.enable_review_human_prs`` is True, this always
        returns True so stage 3 can review human-opened PRs too.
        """
        if self.config.enable_review_human_prs:
            return True
        if not head_ref:
            # Cross-repo PRs and GitHub API gaps arrive with empty head_ref.
            if pr_number:
                logger.warning(
                    "PR #%d has empty head_ref; skipping (cross-repo PR or GitHub API gap). "
                    "Set enable_review_human_prs=True to override.",
                    pr_number,
                )
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
        # Opportunistic cleanup: remove on-disk worktrees for already-merged PRs.
        if self.config.enable_worktree_cleanup and self.worktrees is not None:
            try:
                merged_prs = await self.github.list_recently_merged_prs(limit=100)
                merged_branches = [pr.head_ref for pr in merged_prs if pr.head_ref]
                # PR numbers are public metadata (already in the URL) so a
                # bounded sample is safe to log; head_refs leak branch
                # conventions and stay out.
                dropped_pr_numbers = sorted(
                    pr.number for pr in merged_prs if not pr.head_ref
                )
                if dropped_pr_numbers:
                    logger.debug(
                        "cleanup_merged_branches: dropped %d entries with empty "
                        "head_ref (PRs: %s)",
                        len(dropped_pr_numbers),
                        dropped_pr_numbers[:10],
                    )
                deleted = await self.worktrees.cleanup_merged_branches(merged_branches)
                if deleted:
                    logger.info("startup: cleaned up %d merged worktrees", deleted)
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                logger.warning("startup worktree cleanup failed: %s", exc)

        try:
            schedule = self.schedule_store.load()
        except Exception as exc:  # noqa: BLE001 — schedule errors must not block boot
            logger.warning("schedule load failed; skipping catch-up: %s", exc)
            return

        # GC completed oneshots so the YAML doesn't grow indefinitely.
        removed = schedule.gc_completed_oneshots()
        if removed:
            logger.info("startup: gc'd %d completed oneshots from schedule", removed)

        pending = schedule.compute_pending(
            replay_window_hours=self.config.bootstrap_replay_window_hours
        )
        if not pending:
            try:
                self.schedule_store.save(schedule)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not persist schedule after startup gc: %s", exc)
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
        action_def = ACTIONS_BY_NAME.get(run.action)
        if action_def is None:
            logger.debug("scheduled action %s unknown; skipped", run.action)
            return

        # ``enable_attr`` is documented in :class:`ActionDef` as "must return
        # True" — use ``is not True`` so ``Optional[bool] = None`` and other
        # falsy-but-not-False values are treated as disabled too.
        if getattr(self.config, action_def.enable_attr) is not True:
            # Operator scheduled this action but disabled it in config — warn loudly.
            logger.warning(
                "scheduled action %r is disabled (%s is not True); "
                "skipping run at %s. Remove the schedule entry or re-enable the flag.",
                run.action, action_def.enable_attr, run.when.isoformat(),
            )
            self._stats.skipped_runs += 1
            return

        await getattr(self, action_def.method)()

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
        #
        # If a stage is enabled in config but missing from the schedule's recurring
        # entries, we still run it legacy-style so an incomplete schedule doesn't
        # silently drop stages. Schedule entries override; gaps fall back to legacy.
        # Only-oneshot schedules are respected as-is (no recurring fallback).
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

            if schedule.recurring:
                scheduled_actions = {e.action for e in schedule.recurring if e.enabled}
                if self.config.enable_classify and "classify" not in scheduled_actions:
                    logger.debug("classify not in schedule; running legacy fallback")
                    await self._classify_new_issues()
                if self.config.enable_review and "review" not in scheduled_actions:
                    logger.debug("review not in schedule; running legacy fallback")
                    await self._review_one_new_issue()
                if self.config.enable_implement and "implement" not in scheduled_actions:
                    logger.debug("implement not in schedule; running legacy fallback")
                    await self._implement_one_reviewed_issue()
                if self.config.enable_pr_review and "pr_review" not in scheduled_actions:
                    logger.debug("pr_review not in schedule; running legacy fallback")
                    await self._review_one_open_pr()
                if self.config.enable_pr_triage:
                    await self._classify_new_prs()
                if self.config.enable_mention_handling:
                    await self._process_mentions()
            return

        if self.config.enable_classify:
            await self._classify_new_issues()
        if self.config.enable_review:
            await self._review_one_new_issue()
        if self.config.enable_implement:
            await self._implement_one_reviewed_issue()
        if self.config.enable_pr_review:
            await self._review_one_open_pr()
        if self.config.enable_pr_triage:
            await self._classify_new_prs()
        if self.config.enable_mention_handling:
            await self._process_mentions()

    # ------------------------------------------------------------------
    # stage handlers — thin delegators to ``stages.py``
    #
    # The seven stage handlers below were extracted to ``stages.py`` as free
    # ``async def`` functions taking the monitor as first argument. The
    # methods here remain as thin delegators so existing tests and callers
    # that invoke them via the instance keep working unchanged.
    # ------------------------------------------------------------------

    async def _classify_new_issues(self) -> None:
        return await stages.classify_new_issues(self)

    async def _classify_new_prs(self) -> None:
        return await stages.classify_new_prs(self)

    async def _process_mentions(self) -> None:
        return await stages.process_mentions(self)

    async def _review_one_new_issue(self) -> None:
        return await stages.review_one_new_issue(self)

    async def _implement_one_reviewed_issue(self) -> None:
        return await stages.implement_one_reviewed_issue(self)

    async def _review_one_open_pr(self) -> None:
        return await stages.review_one_open_pr(self)

    async def _stage4_follow_ups(self, pr_number: int, pr_title: str, pr_url: str) -> None:
        return await stages.stage4_follow_ups(self, pr_number, pr_title, pr_url)

    async def _standalone_follow_ups(self) -> None:
        return await stages.standalone_follow_ups(self)

    # ----- mention handling: cursor helpers ----------------------------------

    def _load_mention_cursor(self) -> datetime:
        try:
            if self._mention_cursor is not None:
                return self._mention_cursor
            if self._mention_cursor_path.exists():
                raw = self._mention_cursor_path.read_text().strip()
                return datetime.fromisoformat(raw).astimezone(timezone.utc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention cursor load failed; using 30-min lookback: %s", exc)
        return datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)

    def _save_mention_cursor(self, ts: datetime) -> None:
        try:
            self._mention_cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._mention_cursor_path.write_text(ts.isoformat())
            self._mention_cursor = ts
        except Exception as exc:  # noqa: BLE001
            logger.warning("mention cursor save failed: %s", exc)
