"""Tests for mention handling: _process_mentions() in PipelineMonitor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef,
                                                        PrRef)
from deile.orchestration.pipeline.implementer import WorkOutcome
from deile.orchestration.pipeline.labels import (MENTION_DONE,
                                                 WORKFLOW_ARCHITECTURE,
                                                 WORKFLOW_BLOCKED,
                                                 WORKFLOW_DECOMPOSED,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_WAITING)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)


def _comment(
    comment_id: int,
    body: str,
    *,
    author: str = "user1",
    kind: str = "issue",
) -> CommentRef:
    return CommentRef(
        comment_id=comment_id,
        body=body,
        html_url=f"https://github.com/o/r/issues/1#issuecomment-{comment_id}",
        issue_url="https://api.github.com/repos/o/r/issues/1",
        author=author,
        kind=kind,
    )


def _make_monitor(
    *,
    issue_comments: list | None = None,
    pr_comments: list | None = None,
    claude_ok: bool = True,
) -> tuple[PipelineMonitor, MagicMock, MagicMock]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=list(issue_comments or []))
    github.list_pr_review_comments_since = AsyncMock(return_value=list(pr_comments or []))
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))
    # The mention stage marks sticky triggers ~mention:processado after a
    # successful dispatch (issue #253 cross-tick dedup fix).
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    # Gate integration (issue #257): a comment mention on an issue checks the
    # issue's current ~workflow: state. Default = no labels (not gated) so the
    # legacy one-shot path is preserved; specific tests override this.
    github.get_issue = AsyncMock(
        return_value=IssueRef(number=1, title="t", url="https://github.com/o/r/issues/1", labels=())
    )

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up", "pr_reviewed",
        "issue_auto_classified", "error", "pr_auto_classified", "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    worktrees = MagicMock()
    claude = MagicMock()
    claude.run = AsyncMock(return_value=ClaudeRunResult(
        returncode=0 if claude_ok else 1,
        stdout="done",
        stderr="",
        duration_seconds=0.1,
        cmd=("claude", "-p", "x"),
    ))

    # Issue #309 fase 2: ``build_implementer`` agora sempre constrói
    # ``WorkerImplementer``. Os testes de mention-handling não dependem
    # da estratégia concreta (apenas do contrato ``mention/implement/review``);
    # injetamos um stub que respeita ``claude_ok`` para preservar a semântica
    # dos testes legacy (``claude_ok=False`` ⇒ dispatch falha).
    implementer_stub = MagicMock()
    outcome_ok = WorkOutcome(ok=claude_ok, text="done", error="" if claude_ok else "boom")
    implementer_stub.implement = AsyncMock(return_value=outcome_ok)
    implementer_stub.review = AsyncMock(return_value=outcome_ok)
    implementer_stub.mention = AsyncMock(return_value=outcome_ok)

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=worktrees, claude=claude,
        notifier=notifier, implementer=implementer_stub,
    )
    return monitor, github, notifier


class TestProcessMentions:
    async def test_no_comments_no_dispatch(self):
        monitor, github, notifier = _make_monitor()
        await monitor._process_mentions()
        notifier.mention_processed.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_comment_with_mention_dispatches(self):
        comment = _comment(1, "Hey @deile-one can you help?")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once_with(comment.html_url, comment.author)
        assert monitor.stats.mentions_processed == 1

    async def test_comment_without_mention_skipped(self):
        comment = _comment(2, "Just a regular comment, no mention here")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        notifier.mention_processed.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_mention_in_pr_review_comment_dispatches(self):
        pr_comment = _comment(3, "@deile-one please review this", kind="pr_review")
        monitor, github, notifier = _make_monitor(pr_comments=[pr_comment])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_both_issue_and_pr_comments_polled(self):
        ic = _comment(10, "@deile-one issue mention")
        pc = _comment(11, "@deile-one pr mention", kind="pr_review")
        monitor, github, notifier = _make_monitor(issue_comments=[ic], pr_comments=[pc])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 2
        assert notifier.mention_processed.call_count == 2

    async def test_claude_run_fails_mention_not_counted(self):
        """When claude.run returns non-ok, the mention is NOT counted."""
        comment = _comment(4, "@deile-one do something")
        monitor, github, notifier = _make_monitor(issue_comments=[comment], claude_ok=False)
        await monitor._process_mentions()
        notifier.mention_processed.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_cursor_saved_after_processing(self, tmp_path):
        """After _process_mentions(), the cursor file must exist."""
        cfg = PipelineConfig(
            repo="owner/name",
            base_repo_path=tmp_path,
            notify_user_id="42",
        )
        github = MagicMock()
        github.list_issue_comments_since = AsyncMock(return_value=[])
        github.list_pr_review_comments_since = AsyncMock(return_value=[])
        notifier = MagicMock()
        for attr in ("mention_processed", "error"):
            setattr(notifier, attr, AsyncMock())
        claude = MagicMock()
        claude.run = AsyncMock(return_value=ClaudeRunResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0, cmd=("claude",)
        ))
        monitor = PipelineMonitor(cfg, github=github, worktrees=MagicMock(), claude=claude, notifier=notifier)
        await monitor._process_mentions()
        assert monitor._mention_cursor_path.exists()

    async def test_cursor_case_insensitive_match(self):
        """Mention matching must be case-insensitive."""
        comment = _comment(5, "Hello @DEILE-ONE, please help")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1

    async def test_poll_exception_does_not_crash(self):
        """Exception during GitHub poll must be caught; loop continues cleanly."""
        monitor, github, notifier = _make_monitor()
        github.list_issue_comments_since = AsyncMock(side_effect=RuntimeError("network error"))
        # Should not raise
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_mention_handling_disabled_skips_on_tick(self):
        """When enable_mention_handling=False, _process_mentions is not called on tick."""
        comment = _comment(6, "@deile-one ignored")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        monitor.config.enable_mention_handling = False
        monitor.config.enable_classify = False
        monitor.config.enable_review = False
        monitor.config.enable_implement = False
        monitor.config.enable_pr_review = False
        monitor.config.enable_pr_triage = False
        await monitor.tick()
        github.list_issue_comments_since.assert_not_called()


# ----- Multi-trigger mention handling (issue #253) ------------------------

def _issue_ref(number=100, labels=()):
    return IssueRef(
        number=number, title="test", url=f"https://github.com/o/r/issues/{number}",
        labels=tuple(labels),
    )


def _pr_ref(number=200, labels=()):
    return PrRef(
        number=number, title="pr", url=f"https://github.com/o/r/pull/{number}",
        labels=tuple(labels), head_ref=f"auto/issue-{number}",
    )


class TestProcessMentionsMultiTrigger:
    async def test_assignee_issue_dispatches(self):
        """When DEILE is assigned to an issue, a mention dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(42)])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_assignee_pr_dispatches(self):
        """When DEILE is assigned to a PR, a mention dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_reviewer_request_dispatches(self):
        """When DEILE is requested as reviewer, a mention dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_with_review_requests = AsyncMock(return_value=[_pr_ref(88)])
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_body_mention_issue_dispatches(self):
        """When @deile-one appears in an issue body, a dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(
            return_value=([_issue_ref(55)], [])
        )
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_body_mention_pr_dispatches(self):
        """When @deile-one appears in a PR body, a dispatch fires."""
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(
            return_value=([], [_pr_ref(66)])
        )
        await monitor._process_mentions()
        notifier.mention_processed.assert_called_once()
        assert monitor.stats.mentions_processed == 1

    async def test_dedup_assignee_plus_comment_same_issue(self):
        """Assignee + mention on the SAME issue = single dispatch with full context."""
        comment = _comment(50, "Hey @deile-one fix this")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(1)])
        await monitor._process_mentions()
        # Both triggers target issue #1 → deduped into ONE dispatch
        assert monitor.stats.mentions_processed == 1
        notifier.mention_processed.assert_called_once()

    async def test_dedup_two_comments_same_issue(self):
        """Two @deile-one comments on the same issue = single dispatch."""
        c1 = _comment(100, "@deile-one do X")
        c2 = _comment(101, "@deile-one also Y")
        monitor, github, notifier = _make_monitor(issue_comments=[c1, c2])
        await monitor._process_mentions()
        # Both comments target issue #1 → deduped
        assert monitor.stats.mentions_processed == 1

    async def test_no_dedup_different_issues(self):
        """Mentions on different issues = separate dispatches."""
        c1 = _comment(200, "@deile-one", author="a")
        c2 = _comment(201, "@deile-one", author="b")
        # Change issue_url so they point to different issues
        c2 = CommentRef(
            comment_id=201, body="@deile-one",
            html_url="https://github.com/o/r/issues/2#issuecomment-201",
            issue_url="https://api.github.com/repos/o/r/issues/2",
            author="b", kind="issue",
        )
        monitor, github, notifier = _make_monitor(issue_comments=[c1, c2])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 2

    async def test_assignee_exception_does_not_crash(self):
        """Exception polling assignee must not crash the mention loop."""
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(side_effect=RuntimeError("boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_reviewer_exception_does_not_crash(self):
        """Exception polling reviewers must not crash the mention loop."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_with_review_requests = AsyncMock(side_effect=RuntimeError("boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_body_search_exception_does_not_crash(self):
        """Exception searching bodies must not crash the mention loop."""
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(side_effect=RuntimeError("boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0

    async def test_cursor_saved_with_new_triggers(self, tmp_path):
        """Cursor must be saved even when new trigger types are used."""
        cfg = PipelineConfig(
            repo="owner/name",
            base_repo_path=tmp_path,
            notify_user_id="42",
        )
        github = MagicMock()
        github.list_issue_comments_since = AsyncMock(return_value=[])
        github.list_pr_review_comments_since = AsyncMock(return_value=[])
        github.list_issues_assigned_to = AsyncMock(return_value=[])
        github.list_prs_assigned_to = AsyncMock(return_value=[])
        github.list_prs_with_review_requests = AsyncMock(return_value=[])
        github.search_items_mentioning = AsyncMock(return_value=([], []))
        notifier = MagicMock()
        for attr in ("mention_processed", "error"):
            setattr(notifier, attr, AsyncMock())
        claude = MagicMock()
        claude.run = AsyncMock(return_value=ClaudeRunResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0, cmd=("claude",)
        ))
        monitor = PipelineMonitor(cfg, github=github, worktrees=MagicMock(), claude=claude, notifier=notifier)
        await monitor._process_mentions()
        assert monitor._mention_cursor_path.exists()


# ----- Cross-tick deduplication of sticky triggers (issue #253 storm fix) ----

class TestProcessMentionsCrossTickDedup:
    """The duplicate-DM storm: assignee/reviewer/body triggers re-appear on every
    poll, so without a marker they re-dispatch the same work every tick. The fix
    skips targets carrying ~mention:processado and applies it after a successful
    sticky dispatch. Comments stay governed by the timestamp cursor and ignore
    the label."""

    async def test_assignee_issue_with_mention_done_still_routes(self):
        """Após o refactor "PR é o quadro", ``~mention:processado`` em uma
        issue assignee NÃO bloqueia mais — o gate cross-tick do collector foi
        removido pra issues sticky. Issue assignee continua sendo INJETADA no
        pipeline via ``~workflow:nova`` (não passa pelo implementer.mention).
        """
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(
            return_value=[_issue_ref(42, labels=(MENTION_DONE,))]
        )
        await monitor._process_mentions()
        # Continua sendo roteada — o marker antigo deixa de filtrar.
        assert monitor.stats.mentions_processed == 1
        github.add_labels.assert_any_call("issue", 42, [WORKFLOW_NEW])

    async def test_assignee_issue_routes_into_pipeline(self):
        """Assignee on an issue is INJECTED into the pipeline (~workflow:nova),
        not one-shot dispatched — so it gets resume + auto/issue branch + review."""
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(42)])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1
        github.add_labels.assert_any_call("issue", 42, [WORKFLOW_NEW])
        github.add_labels.assert_any_call("issue", 42, [MENTION_DONE])

    async def test_assignee_issue_already_in_pipeline_not_reclassified(self):
        """If the issue already carries a ~workflow:* label it is gated OUT in
        the collector (issue #483 V1 fix) — no trigger is produced, so no label
        is ever added (neither WORKFLOW_NEW nor MENTION_DONE)."""
        monitor, github, notifier = _make_monitor()
        github.list_issues_assigned_to = AsyncMock(
            return_value=[_issue_ref(42, labels=("~workflow:em_revisao",))]
        )
        await monitor._process_mentions()
        # The collector skips the issue entirely — no label mutations at all.
        github.add_labels.assert_not_called()

    async def test_assignee_pr_dispatch_marks_processed_with_pr_kind(self):
        monitor, github, notifier = _make_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1
        github.add_labels.assert_called_once_with("pr", 77, [MENTION_DONE])

    async def test_reviewer_with_mention_done_still_dispatches(self):
        """Após o refactor "PR é o quadro", o marker ``~mention:processado``
        em uma PR reviewer NÃO bloqueia o re-dispatch. O brief unificado
        agora descobre o estado real (se review está APPROVED em HEAD igual,
        ele comenta curto "sem novidade" e o pipeline re-marca pra cortar
        churn natural). Mudanças reais de estado (HEAD novo) voltam a
        entrar pelo trigger natural."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_with_review_requests = AsyncMock(
            return_value=[_pr_ref(88, labels=(MENTION_DONE,))]
        )
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1
        # PR sticky-success agora SEMPRE marca (não há mais exceção pra reviewer).
        github.add_labels.assert_called_once_with("pr", 88, [MENTION_DONE])

    async def test_body_mention_already_processed_is_skipped(self):
        monitor, github, notifier = _make_monitor()
        github.search_items_mentioning = AsyncMock(
            return_value=([_issue_ref(55, labels=(MENTION_DONE,))], [])
        )
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0
        github.add_labels.assert_not_called()

    async def test_comment_only_does_not_mark_processed(self):
        """A comment-only group must NOT get the label — the cursor dedups it,
        and marking would wrongly suppress a future assignment on the same item."""
        comment = _comment(1, "@deile-one help")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1
        github.add_labels.assert_not_called()

    async def test_comment_plus_assignee_same_issue_routes(self):
        """A mixed group (comment + assignee) on an issue routes into the
        pipeline once (assignee dominates → ~workflow:nova + marked done)."""
        comment = _comment(50, "@deile-one fix this")  # targets issue #1
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(1)])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1
        github.add_labels.assert_any_call("issue", 1, [WORKFLOW_NEW])
        github.add_labels.assert_any_call("issue", 1, [MENTION_DONE])

    async def test_failed_pr_dispatch_does_not_mark_processed(self):
        """A sticky PR trigger whose dispatch FAILS must NOT be marked — it is
        retried next tick (only successful work is marked done)."""
        monitor, github, notifier = _make_monitor(claude_ok=False)
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 0
        github.add_labels.assert_not_called()

    async def test_mark_failure_does_not_crash_loop(self):
        """If marking ~mention:processado fails after a successful PR dispatch,
        the work still counts and the loop continues."""
        monitor, github, notifier = _make_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        github.add_labels = AsyncMock(side_effect=RuntimeError("label boom"))
        await monitor._process_mentions()
        assert monitor.stats.mentions_processed == 1
        notifier.mention_processed.assert_called_once()


# ----- Role → dispatch mode routing (refactor "PR é o quadro") ---------------

class TestProcessMentionsModeRouting:
    """Após o refactor "PR é o quadro", qualquer trigger sobre uma PR resolve
    para o mode unificado ``pr_unified`` — quem decide o que fazer é o brief
    unificado, olhando o estado real da PR. Os 3 modes antigos
    (``work_merge`` / ``review_only`` / ``address``) deixaram de existir."""

    def _spy_monitor(self, **kw):
        monitor, github, notifier = _make_monitor(**kw)
        monitor.implementer = MagicMock()
        monitor.implementer.mention = AsyncMock(
            return_value=WorkOutcome(ok=True, text="done")
        )
        return monitor, github, notifier

    async def test_pr_reviewer_uses_pr_unified(self):
        monitor, github, notifier = self._spy_monitor()
        github.list_prs_with_review_requests = AsyncMock(return_value=[_pr_ref(88)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_assignee_uses_pr_unified(self):
        monitor, github, notifier = self._spy_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_comment_uses_pr_unified(self):
        pr_comment = _comment(9, "@deile-one tweak X", kind="pr_review")
        monitor, github, notifier = self._spy_monitor(pr_comments=[pr_comment])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_pr_assignee_plus_reviewer_same_mode(self):
        """Qualquer combinação de triggers PR → o mesmo mode unificado."""
        monitor, github, notifier = self._spy_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        github.list_prs_with_review_requests = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"

    async def test_issue_assignee_does_not_dispatch(self):
        """Issue assignee ROUTES (no implementer.mention call) — pipeline takes over."""
        monitor, github, notifier = self._spy_monitor()
        github.list_issues_assigned_to = AsyncMock(return_value=[_issue_ref(42)])
        await monitor._process_mentions()
        monitor.implementer.mention.assert_not_called()
        github.add_labels.assert_any_call("issue", 42, [WORKFLOW_NEW])

    async def test_pr_reviewer_marks_mention_done(self):
        """Após o refactor "PR é o quadro", todo sticky-success em PR aplica
        ``~mention:processado`` — não há mais exceção pra reviewer-only.
        Mudanças reais de estado (HEAD novo) voltam a entrar pelo trigger
        natural; o marker apenas evita re-dispatch redundante."""
        monitor, github, notifier = self._spy_monitor()
        github.list_prs_with_review_requests = AsyncMock(return_value=[_pr_ref(88)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"
        assert monitor.stats.mentions_processed == 1
        github.add_labels.assert_called_once_with("pr", 88, [MENTION_DONE])

    async def test_pr_assignee_marks_mention_done(self):
        """Assignee em PR também marca ``~mention:processado`` em sticky-success."""
        monitor, github, notifier = self._spy_monitor()
        github.list_prs_assigned_to = AsyncMock(return_value=[_pr_ref(77)])
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "pr_unified"
        github.add_labels.assert_called_once_with("pr", 77, [MENTION_DONE])


class TestCommentMentionGateIntegration:
    """Issue #257: a comment mention must NOT pull an issue out of the flow it is
    already in. Mentioning the target by name in a comment is normal."""

    async def test_comment_on_gated_issue_does_not_one_shot(self):
        # Issue is mid-gate (em_arquitetura) → the comment must not spawn a
        # parallel one-shot implementation.
        comment = _comment(1, "boa @deile-one, segue a opção A")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_ARCHITECTURE, "refinar"),
        ))
        await monitor._process_mentions()
        monitor.claude.run.assert_not_called()  # no one-shot dispatch

    async def test_comment_on_waiting_issue_lifts_the_pause(self):
        # Issue paused for the stakeholder → the comment IS the decision: lift the
        # waiting overlay (resume refine), no one-shot.
        comment = _comment(1, "@deile-one decisão: opção C")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_ARCHITECTURE, WORKFLOW_WAITING, "refinar"),
        ))
        await monitor._process_mentions()
        github.remove_labels.assert_any_await("issue", 1, [WORKFLOW_WAITING])
        monitor.claude.run.assert_not_called()

    async def test_comment_on_ungated_issue_still_one_shot(self):
        # No ~workflow: label → standalone request → one-shot preserved (legacy).
        comment = _comment(1, "@deile-one cria um script aí")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        # default get_issue returns labels=() (not gated)
        await monitor._process_mentions()
        # Issue #309 fase 2: o dispatch agora vai pelo implementer stub
        # (``WorkerImplementer.mention``), não mais por ``monitor.claude.run``
        # diretamente. A semântica do teste (one-shot dispatch ocorreu) é
        # preservada validando que o implementer.mention foi chamado.
        monitor.implementer.mention.assert_called()  # one-shot dispatched


class TestCommentMentionTerminalStates:
    """Issue #442: a comment on an issue whose state has NO future gate dispatch
    (em_pr / decomposta / blocked / closed) must be handled one-shot — NEVER
    silently dropped. Dropping it stranded the comment forever once the mention
    cursor advanced past it (and removing the label did not recover it)."""

    async def test_comment_on_em_pr_issue_routes_one_shot(self):
        # The exact #442 scenario: comment on an issue parked in ~workflow:em_pr.
        comment = _comment(1, "@deile-one ainda falta o teste X")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_PR,), state="open",
        ))
        await monitor._process_mentions()
        monitor.implementer.mention.assert_called()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "comment"
        assert monitor.stats.mentions_processed == 1

    async def test_comment_on_decomposed_issue_routes_one_shot(self):
        comment = _comment(1, "@deile-one adiciona a derivada Y")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_DECOMPOSED,), state="open",
        ))
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "comment"
        assert monitor.stats.mentions_processed == 1

    async def test_comment_on_closed_gated_issue_routes_one_shot(self):
        # A CLOSED issue carrying a re-dispatched label still has NO live stage
        # (every stage queries OPEN issues), so the comment must one-shot, not DROP.
        comment = _comment(1, "@deile-one reabre e ajusta Z")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_IMPLEMENTING,), state="closed",
        ))
        await monitor._process_mentions()
        assert monitor.implementer.mention.call_args.kwargs["mode"] == "comment"
        assert monitor.stats.mentions_processed == 1

    async def test_comment_on_open_redispatched_issue_still_defers(self):
        # Regression guard: an OPEN issue in a re-dispatched state still DEFERS
        # (no one-shot) — the gate's worker reads the comment on its next pass.
        comment = _comment(1, "@deile-one segue a opção A")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_REVIEWED,), state="open",
        ))
        await monitor._process_mentions()
        monitor.implementer.mention.assert_not_called()
        assert monitor.stats.mentions_processed == 0

    async def test_comment_on_blocked_issue_defers_silently(self):
        # Blocked is human-gated → DEFER (drop): NO one-shot AND NO status
        # comment. Posting a status per tick + one-shotting created an infinite
        # loop (incident #446 — the mention re-fires each tick). The human sees
        # their own comment; removing ~workflow:bloqueada resumes the flow.
        comment = _comment(1, "@deile-one aqui está a info que faltava")
        monitor, github, notifier = _make_monitor(issue_comments=[comment])
        github.comment_on_issue = AsyncMock()
        github.get_issue = AsyncMock(return_value=IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1",
            labels=(WORKFLOW_BLOCKED,), state="open",
        ))
        await monitor._process_mentions()
        monitor.implementer.mention.assert_not_called()   # no one-shot
        github.comment_on_issue.assert_not_called()        # no status spam
        assert monitor.stats.mentions_processed == 0


class TestPrBlockedMentionGuard:
    """Issue #442 audit: a blocked PR is human-gated (pr_review EXCLUDES it from
    its candidate set). A mention must NOT auto-dispatch pr_unified — which could
    merge a PR the human deliberately blocked. It surfaces the status and stops."""

    def _spy_monitor(self, **kw):
        monitor, github, notifier = _make_monitor(**kw)
        monitor.implementer = MagicMock()
        monitor.implementer.mention = AsyncMock(
            return_value=WorkOutcome(ok=True, text="done")
        )
        return monitor, github, notifier

    async def test_comment_on_blocked_pr_does_not_dispatch(self):
        pr_comment = _comment(1, "@deile-one mergeia aí", kind="pr_review")
        monitor, github, notifier = self._spy_monitor(pr_comments=[pr_comment])
        github.comment_on_pr = AsyncMock()
        github.get_pr = AsyncMock(return_value=_pr_ref(1, labels=(WORKFLOW_BLOCKED,)))
        await monitor._process_mentions()
        monitor.implementer.mention.assert_not_called()   # no dispatch
        github.comment_on_pr.assert_not_called()           # no status spam (anti-loop)
        assert monitor.stats.mentions_processed == 0
