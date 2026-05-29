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
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef,
                                                        MentionTrigger)
from deile.orchestration.pipeline.implementer import (ClaudeImplementer,
                                                      WorkerImplementer,
                                                      WorkOutcome,
                                                      build_implementer)


def _make_monitor(*, claude_stdout="", claude_rc=0, worktree_raises=False):
    monitor = MagicMock()
    monitor.config = SimpleNamespace(
        repo="owner/name", main_branch="main", base_repo_path=Path("/tmp/fake"),
        mention_handle="@deile-one",
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
        body="@deile-one ol\u221a\u00b0", author="someone",
    )


def _mention_trigger_comment(*, trigger_type: str = "comment") -> MentionTrigger:
    """Build a MentionTrigger wrapping a synthetic CommentRef."""
    comment = CommentRef(
        comment_id=1,
        body="@deile-one ola",
        html_url="https://github.com/owner/name/issues/1#issuecomment-1",
        issue_url="https://api.github.com/repos/owner/name/issues/1",
        author="someone",
        kind="issue",
    )
    return MentionTrigger(trigger_type=trigger_type, comment=comment)


def _mention_trigger_assignee_issue(number: int = 100) -> MentionTrigger:
    """Build a MentionTrigger for an assignee on an issue."""
    issue = IssueRef(
        number=number,
        title="test issue",
        url=f"https://github.com/owner/name/issues/{number}",
        labels=(),
    )
    return MentionTrigger(trigger_type="assignee", issue=issue)


# ----- factory ------------------------------------------------------------

class TestFactory:
    """A partir da fase 2 da issue #309, ``build_implementer`` SEMPRE retorna
    :class:`WorkerImplementer` — a decisão de endpoint (``deile-worker`` vs
    ``claude-worker``) é per-stage em runtime via ``dispatch_resolver``. O
    parâmetro ``dispatch_mode`` continua aceito apenas para validar typos
    (fail-fast) e manter compat com chamadas antigas.

    Pré-#309-fase-2: aliases ``claude*`` retornavam :class:`ClaudeImplementer`.
    Agora retornam :class:`WorkerImplementer`. Para construir o legacy
    ClaudeImplementer (CLI local fora do cluster), use
    :func:`get_local_claude_implementer`.
    """

    @pytest.mark.parametrize("mode", ["claude", "claude_code", "claude-code"])
    def test_claude_aliases_return_worker_implementer(self, mode):
        # Mudança semântica de #309 fase 2: aliases ``claude*`` não constroem
        # mais ``ClaudeImplementer``. A escolha de endpoint é runtime via
        # ``dispatch_resolver`` — ``WorkerImplementer`` resolve per-call.
        impl = build_implementer(mode, worker_client=MagicMock())
        assert isinstance(impl, WorkerImplementer)

    @pytest.mark.parametrize("mode", ["deile_worker", "worker", "deile", "deile-worker"])
    def test_worker_aliases(self, mode):
        impl = build_implementer(mode, worker_client=MagicMock())
        assert isinstance(impl, WorkerImplementer)

    def test_unknown_mode_raises(self):
        # Pre-#309-fase-2 esta entrada caía silenciosamente em ClaudeImplementer
        # com logger.warning — um typo em DEILE_PIPELINE_DISPATCH_MODE (ex.:
        # "deile_woker") queimaria ANTHROPIC_API_KEY sem alerta. Fail-fast
        # ValueError surface o erro imediatamente (pilar 03 §6 + dispatch UX).
        with pytest.raises(ValueError, match="unknown pipeline dispatch_mode"):
            build_implementer("nonsense")

    def test_empty_mode_returns_worker_implementer(self):
        # Default vazio/None: WorkerImplementer (resolver runtime decide endpoint).
        impl = build_implementer("")
        assert isinstance(impl, WorkerImplementer)

    def test_none_mode_returns_worker_implementer(self):
        # Sem argumento — mesmo comportamento de vazio.
        impl = build_implementer()
        assert isinstance(impl, WorkerImplementer)

    def test_get_local_claude_implementer_returns_claude(self):
        """Factory exclusiva para uso local fora do cluster (CLI). Continua
        construindo :class:`ClaudeImplementer` (subprocess ``claude -p``)."""
        from deile.orchestration.pipeline.implementer import \
            get_local_claude_implementer
        impl = get_local_claude_implementer()
        assert isinstance(impl, ClaudeImplementer)


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
        trigger = _mention_trigger_comment()
        out = await ClaudeImplementer().mention(
            monitor, trigger,
            trigger_types=["comment"],
            all_triggers=[trigger],
        )
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
        # Issue #373: implement() now dispatches fire-and-forget (nowait=True).
        # The worker returns 202 + task_id; the response has no summary.
        client = _FakeClient({"task_id": "abc123", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.implement(_make_monitor(), _issue(number=242, title="soma", body="impl"))
        assert out.ok is True
        assert out.task_id == "abc123"
        # Fire-and-forget: no summary text in response.
        assert out.text == ""
        assert client.last_payload["channel_id"] == "pipeline-issue-242"
        assert client.last_wait is False
        # Implementation runs under the developer persona.
        assert client.last_payload["persona"] == "developer"
        # The brief must name the repo, the issue number and the branch.
        brief = client.last_payload["brief"]
        assert "owner/name" in brief
        assert "#242" in brief
        assert "auto/issue-242" in brief

    async def test_implement_worker_failure_returns_not_ok(self):
        # Issue #373: fire-and-forget dispatch — transport errors still
        # propagate (the _post_dispatch call itself can fail).
        from deile.infrastructure.deile_worker_client import \
            WorkerDispatchError
        client = _FakeClient(WorkerDispatchError("nope", error_code="WORKER_TIMEOUT"))
        out = await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert out.ok is False
        assert "WORKER_TIMEOUT" in out.error

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
        # The review/merge stage is the final quality gate: it runs under the
        # dedicated ``reviewer`` persona, not ``developer``.
        assert client.last_payload["persona"] == "reviewer"
        # Após o refactor "PR é o quadro" o brief unificado substitui o brief
        # de QUALITY GATE. Asserts agora cobrem o princípio do brief: descoberta
        # de estado real + checkpoint obrigatório de comentário visível.
        brief = client.last_payload["brief"]
        assert "PASSO 0" in brief
        assert "estado real" in brief.lower() or "ESTADO REAL" in brief

    async def test_review_resume_uses_reviewer_persona(self):
        client = _FakeClient({"ok": True, "summary": "https://github.com/owner/name/pull/7 MERGED"})
        out = await WorkerImplementer(client=client).review(
            _make_monitor(), _pr(number=7), resume=True
        )
        assert out.ok is True
        assert client.last_payload["persona"] == "reviewer"

    async def test_mention_dispatches_to_mention_channel(self):
        client = _FakeClient({"ok": True, "summary": "respondido"})
        trigger = _mention_trigger_comment()
        out = await WorkerImplementer(client=client).mention(
            _make_monitor(), trigger,
            trigger_types=["comment"],
            all_triggers=[trigger],
        )
        assert out.ok is True
        assert client.last_payload["channel_id"] == "pipeline-mention-issue-1"
        # Mentions run under the developer persona (only PR review uses reviewer).
        assert client.last_payload["persona"] == "developer"


class TestWorkOutcome:
    def test_defaults(self):
        o = WorkOutcome(ok=True, text="x")
        assert o.error == ""
