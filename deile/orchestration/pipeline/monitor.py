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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from deile.orchestration.pipeline.claude_dispatcher import (
    ClaudeDispatcher, render_implement_prompt, render_review_prompt)
from deile.orchestration.pipeline.github_client import (GhCommandError,
                                                        GitHubClient,
                                                        IssueRef, PrRef)
from deile.orchestration.pipeline.labels import (REVIEW_CONCLUDED,
                                                 REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING, WORKFLOW_NEW,
                                                 WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)
from deile.orchestration.pipeline.notifier import DiscordNotifier
from deile.orchestration.pipeline.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+", re.IGNORECASE)


@dataclass
class PipelineConfig:
    repo: str
    base_repo_path: Path
    poll_interval_seconds: int = 60
    main_branch: str = "main"
    branch_prefix: str = "auto/issue-"
    notify_user_id: Optional[str] = None
    enable_review: bool = True
    enable_implement: bool = True
    enable_pr_review: bool = True


@dataclass
class _Stats:
    ticks: int = 0
    issues_reviewed: int = 0
    issues_implemented: int = 0
    prs_reviewed: int = 0
    errors: int = 0


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
    ) -> None:
        self.config = config
        self.github = github or GitHubClient(config.repo)
        self.worktrees = worktrees or WorktreeManager(
            config.base_repo_path, main_branch=config.main_branch
        )
        self.claude = claude or ClaudeDispatcher()
        self.notifier = notifier or DiscordNotifier(config.notify_user_id)
        # `review_callback` lets a host inject DEILE's actual review function.
        # When None, the monitor performs a no-op "mark as reviewed" pass — the
        # body of the issue is left as-is (useful in tests / dry runs).
        self._review_cb = review_callback
        self._stats = _Stats()
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @property
    def stats(self) -> _Stats:
        return self._stats

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.github.ensure_pipeline_labels()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever(), name="pipeline-monitor")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

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

    # ------------------------------------------------------------------
    # one tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        self._stats.ticks += 1
        logger.debug("pipeline tick #%d", self._stats.ticks)
        if self.config.enable_review:
            await self._review_one_new_issue()
        if self.config.enable_implement:
            await self._implement_one_reviewed_issue()
        if self.config.enable_pr_review:
            await self._review_one_open_pr()

    # ----- stage 1: review ------------------------------------------

    async def _review_one_new_issue(self) -> None:
        try:
            issues = await self.github.list_issues_with_label(WORKFLOW_NEW, limit=50)
        except GhCommandError as exc:
            logger.warning("could not list new issues: %s", exc)
            return
        target = next((i for i in issues if i.batch_id is None), None)
        if target is None:
            return
        batch = await self.github.claim_with_batch("issue", target.number, target.title)
        if batch is None:
            return
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
        target = next(
            (i for i in issues if WORKFLOW_PR not in i.labels and i.batch_id is not None),
            None,
        )
        if target is None:
            return
        branch = f"{self.config.branch_prefix}{target.number}"
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
                f"implement #{target.number}", result.stderr.strip()[:1500] or "non-zero exit"
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
        target = next(
            (
                pr
                for pr in prs
                if not pr.is_draft
                and REVIEW_CONCLUDED not in pr.labels
                and REVIEW_IN_PROGRESS not in pr.labels
                and pr.batch_id is None
            ),
            None,
        )
        if target is None:
            return
        batch = await self.github.claim_with_batch("pr", target.number, target.title)
        if batch is None:
            return
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
