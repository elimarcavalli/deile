"""Tests for make_post_merge_callback() factory and PipelineMonitor integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import PrRef
from deile.orchestration.pipeline.implementer import WorkOutcome
from deile.orchestration.pipeline.labels import REVIEW_PENDING
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.post_merge_callback import \
    make_post_merge_callback


class TestMakePostMergeCallback:
    def test_returns_none_when_agent_is_none(self):
        cb = make_post_merge_callback(None)
        assert cb is None

    def test_returns_callable_when_agent_provided(self):
        agent = MagicMock()
        cb = make_post_merge_callback(agent)
        assert callable(cb)

    async def test_callback_calls_store_episode_with_correct_args(self):
        episodic = MagicMock()
        episodic.store_episode = AsyncMock()
        mem = MagicMock()
        mem.episodic_memory = episodic
        agent = MagicMock()
        agent.memory_manager = mem

        cb = make_post_merge_callback(agent)
        assert cb is not None
        await cb(99, "fix: parser edge case", "https://github.com/o/r/pull/99")

        episodic.store_episode.assert_called_once()
        call_kwargs = episodic.store_episode.call_args.kwargs
        assert "PR #99" in call_kwargs["user_input"]
        assert "fix: parser edge case" in call_kwargs["user_input"]
        assert call_kwargs["agent_response"] == "[pipeline:merge]"
        assert call_kwargs["context"]["type"] == "pr_merged"
        assert call_kwargs["context"]["pr_number"] == 99
        assert call_kwargs["context"]["pr_url"] == "https://github.com/o/r/pull/99"
        assert call_kwargs["session_id"] == "pipeline-merge-99"

    async def test_callback_noop_when_memory_manager_missing(self):
        agent = MagicMock(spec=[])  # no memory_manager attribute
        cb = make_post_merge_callback(agent)
        assert cb is not None
        # Should not raise
        await cb(1, "title", "url")

    async def test_callback_noop_when_episodic_memory_missing(self):
        mem = MagicMock(spec=[])  # no episodic_memory attribute
        agent = MagicMock()
        agent.memory_manager = mem
        cb = make_post_merge_callback(agent)
        assert cb is not None
        # Should not raise
        await cb(2, "title", "url")

    async def test_callback_swallows_store_episode_exception(self):
        episodic = MagicMock()
        episodic.store_episode = AsyncMock(side_effect=RuntimeError("db error"))
        mem = MagicMock()
        mem.episodic_memory = episodic
        agent = MagicMock()
        agent.memory_manager = mem

        cb = make_post_merge_callback(agent)
        assert cb is not None
        # Must not raise — best-effort
        await cb(3, "title", "url")


# ---------------------------------------------------------------------------
# Integration: PipelineMonitor calls post_merge_callback after PR merge
# ---------------------------------------------------------------------------

def _make_monitor_with_cb(
    *,
    claude_stdout: str = "merged.",
    claude_rc: int = 0,
    post_merge_callback=None,
) -> tuple[PipelineMonitor, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[
        PrRef(
            number=77, title="feat: something", url="https://github.com/o/r/pull/77",
            labels=(REVIEW_PENDING,), head_ref="auto/issue-5",
        )
    ])
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.clear_batch_label = AsyncMock()
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up", "pr_reviewed",
        "issue_auto_classified", "follow_ups_processed", "error",
        "pr_auto_classified", "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    worktrees = MagicMock()
    from deile.orchestration.pipeline.worktree_manager import Worktree
    worktrees.create_branch_worktree = AsyncMock(
        return_value=Worktree(path=Path("/tmp/fake/.wt"), branch="x", base_repo=Path("/tmp/fake"))
    )

    claude = MagicMock()
    claude.run = AsyncMock(return_value=ClaudeRunResult(
        returncode=claude_rc,
        stdout=claude_stdout,
        stderr="",
        duration_seconds=0.1,
        cmd=("claude", "-p", "x"),
    ))

    # Issue #309 fase 2: build_implementer sempre retorna WorkerImplementer.
    # Stub que respeita claude_rc/claude_stdout para manter semântica do teste.
    outcome_for_test = WorkOutcome(
        ok=(claude_rc == 0),
        text=claude_stdout,
        error="" if claude_rc == 0 else "boom",
    )
    implementer_stub = MagicMock()
    implementer_stub.implement = AsyncMock(return_value=outcome_for_test)
    implementer_stub.review = AsyncMock(return_value=outcome_for_test)
    implementer_stub.mention = AsyncMock(return_value=outcome_for_test)

    monitor = PipelineMonitor(
        cfg,
        github=github,
        worktrees=worktrees,
        claude=claude,
        notifier=notifier,
        post_merge_callback=post_merge_callback,
        implementer=implementer_stub,
    )
    monitor.config.enable_review = False
    monitor.config.enable_implement = False
    return monitor, notifier


class TestMonitorCallsPostMergeCallback:
    async def test_callback_called_after_merge(self):
        called_with: list = []

        async def _cb(pr_number: int, pr_title: str, pr_url: str) -> None:
            called_with.append((pr_number, pr_title, pr_url))

        monitor, _ = _make_monitor_with_cb(claude_stdout="merged.", post_merge_callback=_cb)
        await monitor.tick()
        assert len(called_with) == 1
        assert called_with[0][0] == 77
        assert called_with[0][1] == "feat: something"

    async def test_callback_not_called_when_not_merged(self):
        called_with: list = []

        async def _cb(pr_number: int, pr_title: str, pr_url: str) -> None:
            called_with.append((pr_number, pr_title, pr_url))

        monitor, _ = _make_monitor_with_cb(claude_stdout="review done", post_merge_callback=_cb)
        await monitor.tick()
        assert called_with == []

    async def test_callback_failure_does_not_propagate(self):
        async def _cb(pr_number: int, pr_title: str, pr_url: str) -> None:
            raise RuntimeError("episodic db unavailable")

        monitor, _ = _make_monitor_with_cb(claude_stdout="merged.", post_merge_callback=_cb)
        # Must not raise
        await monitor.tick()
        assert monitor.stats.prs_reviewed == 1
