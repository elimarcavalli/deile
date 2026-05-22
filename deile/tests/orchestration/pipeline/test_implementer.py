"""Unit tests for the pluggable pipeline implementer strategy.

Covers the factory selection, the Claude strategy (delegates to the injected
``monitor.claude`` + ``monitor.worktrees``) and the deile-worker strategy
(builds the brief, picks the synthetic channel, parses the worker response).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.implementer import (ClaudeImplementer,
                                                      WorkerImplementer,
                                                      WorkOutcome,
                                                      build_implementer)


def _make_monitor(*, claude_stdout="", claude_rc=0, worktree_raises=False):
    monitor = MagicMock()
    monitor.config = SimpleNamespace(
        repo="owner/name", main_branch="main", base_repo_path=Path("/tmp/fake")
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    if worktree_raises:
        monitor.worktrees.create_branch_worktree = AsyncMock(
            side_effect=RuntimeError("boom")
        )
    else:
        monitor.worktrees.create_branch_worktree = AsyncMock(
            return_value=SimpleNamespace(path=Path("/tmp/fake/.worktrees/x"))
        )
    monitor.claude.run = AsyncMock(
        return_value=ClaudeRunResult(
            returncode=claude_rc, stdout=claude_stdout, stderr="err" if claude_rc else "",
            duration_seconds=0.1, cmd=("claude", "-p", "x"),
        )
    )
    return monitor


def _issue(number=242, title="t", body="b"):
    return SimpleNamespace(number=number, title=title, body=body)


def _pr(number=7, title="t", head_ref="auto/issue-242"):
    return SimpleNamespace(
        number=number, title=title, head_ref=head_ref,
        url=f"https://github.com/owner/name/pull/{number}",
    )


def _comment():
    return SimpleNamespace(
        html_url="https://github.com/owner/name/issues/1#c1",
        body="@deile-one olá", author="someone",
    )


# ----- factory ------------------------------------------------------------

class TestFactory:
    @pytest.mark.parametrize("mode", ["claude", "claude_code", "claude-code"])
    def test_claude_aliases(self, mode):
        assert isinstance(build_implementer(mode), ClaudeImplementer)

    @pytest.mark.parametrize("mode", ["deile_worker", "worker", "deile", "deile-worker"])
    def test_worker_aliases(self, mode):
        impl = build_implementer(mode, worker_client=MagicMock())
        assert isinstance(impl, WorkerImplementer)

    def test_unknown_mode_falls_back_to_claude(self):
        assert isinstance(build_implementer("nonsense"), ClaudeImplementer)

    def test_empty_mode_falls_back_to_claude(self):
        assert isinstance(build_implementer(""), ClaudeImplementer)


# ----- ClaudeImplementer --------------------------------------------------

class TestClaudeImplementer:
    async def test_implement_uses_worktree_and_claude(self):
        monitor = _make_monitor(claude_stdout="https://github.com/owner/name/pull/9")
        out = await ClaudeImplementer().implement(monitor, _issue())
        assert out.ok is True
        assert "pull/9" in out.text
        monitor.worktrees.create_branch_worktree.assert_awaited_once()
        monitor.claude.run.assert_awaited_once()

    async def test_implement_worktree_failure_returns_not_ok(self):
        monitor = _make_monitor(worktree_raises=True)
        out = await ClaudeImplementer().implement(monitor, _issue())
        assert out.ok is False
        assert "worktree" in out.error
        monitor.claude.run.assert_not_awaited()

    async def test_implement_claude_nonzero_returns_not_ok(self):
        monitor = _make_monitor(claude_rc=2)
        out = await ClaudeImplementer().implement(monitor, _issue())
        assert out.ok is False

    async def test_review_uses_worktree_and_claude(self):
        monitor = _make_monitor(claude_stdout="merged https://github.com/owner/name/pull/9")
        out = await ClaudeImplementer().review(monitor, _pr())
        assert out.ok is True
        assert "merged" in out.text.lower()

    async def test_mention_runs_in_base_repo_path(self):
        monitor = _make_monitor(claude_stdout="done")
        out = await ClaudeImplementer().mention(monitor, _comment())
        assert out.ok is True
        _, kwargs = monitor.claude.run.call_args
        assert kwargs["cwd"] == monitor.config.base_repo_path


# ----- WorkerImplementer --------------------------------------------------

class _FakeClient:
    """Captures the dispatch payload and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.last_payload = None
        self.last_wait = None

    async def dispatch(self, payload, *, wait):
        self.last_payload = payload
        self.last_wait = wait
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestWorkerImplementer:
    async def test_implement_dispatches_brief_and_parses_ok(self):
        client = _FakeClient({"ok": True, "summary": "Feito.\nhttps://github.com/owner/name/pull/12"})
        impl = WorkerImplementer(client=client)
        out = await impl.implement(_make_monitor(), _issue(number=242, title="soma", body="impl"))
        assert out.ok is True
        assert "pull/12" in out.text
        assert client.last_payload["channel_id"] == "pipeline-issue-242"
        assert client.last_wait is True
        # The brief must name the repo, the issue number and the branch.
        brief = client.last_payload["brief"]
        assert "owner/name" in brief
        assert "#242" in brief
        assert "auto/issue-242" in brief

    async def test_implement_worker_failure_returns_not_ok(self):
        client = _FakeClient({"ok": False, "summary": "erro: deu ruim", "error": "boom"})
        out = await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert out.ok is False
        assert out.error

    async def test_dispatch_error_is_caught(self):
        from deile.infrastructure.deile_worker_client import \
            WorkerDispatchError
        client = _FakeClient(WorkerDispatchError("nope", error_code="WORKER_TIMEOUT"))
        out = await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert out.ok is False
        assert "WORKER_TIMEOUT" in out.error

    async def test_review_uses_pr_channel_and_merged_marker(self):
        client = _FakeClient({"ok": True, "summary": "https://github.com/owner/name/pull/7 MERGED"})
        out = await WorkerImplementer(client=client).review(_make_monitor(), _pr(number=7))
        assert out.ok is True
        assert "merged" in out.text.lower()
        assert client.last_payload["channel_id"] == "pipeline-pr-7"

    async def test_mention_dispatches_to_mentions_channel(self):
        client = _FakeClient({"ok": True, "summary": "respondido"})
        out = await WorkerImplementer(client=client).mention(_make_monitor(), _comment())
        assert out.ok is True
        assert client.last_payload["channel_id"] == "pipeline-mentions"


class TestWorkOutcome:
    def test_defaults(self):
        o = WorkOutcome(ok=True, text="x")
        assert o.error == ""
