"""FIX #5 — mention PR-unified dispatch deve ser fire-and-forget (nowait).

Regresso de produção: PR #518 travou o tick por 18 min porque o dispatch de
mention PR-unified usava wait=True (bloqueante). O dispatch de mention deve
retornar imediatamente (202 + task_id), como já faz o pr_review FRESH.

Garante que:
1. WorkerImplementer.mention(mode="pr_unified") chama _post_dispatch com
   wait=False (nowait=True no _dispatch).
2. O modo "comment" (issue mention) não é afetado.
3. O guard de concorrência (CONCURRENT_DISPATCH_BLOCKED) é preservado.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef,
                                                        PrRef)
from deile.orchestration.pipeline.implementer import (WorkerImplementer,
                                                      WorkOutcome)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CapturingClient:
    """Cliente fake que captura os kwargs do dispatch."""

    def __init__(self):
        self.calls: list[dict] = []
        self._seq = 0

    async def dispatch(self, payload, *, wait, endpoint_url=None):
        self._seq += 1
        self.calls.append({"wait": wait, "payload": payload})
        if not wait:
            # Nowait: retorna 202 + task_id imediatamente.
            return {"task_id": f"t-{self._seq:03d}", "status": "running"}
        # Wait: retorna resultado bloqueante.
        return {"ok": True, "summary": "done"}


def _make_implementer(client=None):
    client = client or _CapturingClient()
    ledger = DispatchLedger(path=Path(tempfile.mkdtemp()) / "dispatches.json")
    return WorkerImplementer(client=client, ledger=ledger), client


def _make_monitor(implementer=None):
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
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.get_issue = AsyncMock(
        return_value=IssueRef(
            number=1, title="t",
            url="https://github.com/o/r/issues/1", labels=(),
        )
    )
    github.get_pr = AsyncMock(return_value=None)

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up",
        "pr_reviewed", "issue_auto_classified", "error", "pr_auto_classified",
        "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    impl = implementer or MagicMock()
    if isinstance(impl, MagicMock):
        impl.mention = AsyncMock(return_value=WorkOutcome(ok=True, text="done"))

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=MagicMock(),
        claude=MagicMock(), notifier=notifier, implementer=impl,
    )
    return monitor, github


def _pr_ref(number=200, labels=()):
    return PrRef(
        number=number, title="pr",
        url=f"https://github.com/o/r/pull/{number}",
        labels=tuple(labels),
        head_ref=f"auto/issue-{number}",
    )


def _make_pr_mention_ref():
    """Constrói um MentionTrigger de PR-assignee."""
    from deile.orchestration.pipeline.stages import MentionTrigger
    pr = _pr_ref(99)
    return MentionTrigger(trigger_type="assignee", pr=pr)


# ---------------------------------------------------------------------------
# FIX #5: WorkerImplementer.mention(mode="pr_unified") deve ser nowait
# ---------------------------------------------------------------------------

class TestMentionPrUnifiedIsNowait:
    """Garante que o dispatch de mention PR-unified é não-bloqueante."""

    async def test_pr_mention_dispatch_uses_wait_false(self):
        """mention(mode='pr_unified') → _post_dispatch com wait=False.

        O cliente fake captura o kwarg wait. O fix está em implementer.py:
        _dispatch(..., nowait=True) para mode='pr_unified'.
        """
        impl, client = _make_implementer()
        monitor, github = _make_monitor(implementer=impl)

        ref = _make_pr_mention_ref()
        await impl.mention(monitor, ref, trigger_types=["assignee"], mode="pr_unified")

        assert len(client.calls) == 1, "dispatch deve ter sido chamado exatamente uma vez"
        call = client.calls[0]
        # FIX #5: deve ser wait=False (fire-and-forget), não wait=True (bloqueante).
        assert call["wait"] is False, (
            f"mention PR-unified deveria usar wait=False (nowait), "
            f"mas usou wait={call['wait']!r}. "
            "Isso trava o tick enquanto o claude processa a PR."
        )

    async def test_pr_mention_dispatch_returns_immediately_with_task_id(self):
        """mention(mode='pr_unified') retorna WorkOutcome(ok=True, task_id=...)."""
        impl, client = _make_implementer()
        monitor, github = _make_monitor(implementer=impl)

        ref = _make_pr_mention_ref()
        outcome = await impl.mention(monitor, ref, trigger_types=["assignee"], mode="pr_unified")

        assert outcome.ok is True
        assert outcome.task_id, "task_id deve estar preenchido no caminho nowait"

    async def test_comment_mention_dispatch_wait_is_unchanged(self):
        """mention(mode='comment') deve continuar usando wait=True (bloqueante curto).

        O brief de comment é simples e síncrono — não tem o mesmo risco de hang.
        Mas se o projeto quiser mudar isso também no futuro, o teste deve ser
        atualizado explicitamente (não silenciosamente).
        """
        impl, client = _make_implementer()
        monitor, github = _make_monitor(implementer=impl)

        from deile.orchestration.pipeline.stages import MentionTrigger

        comment = CommentRef(
            comment_id=1, body="@deile-one help",
            html_url="https://github.com/o/r/issues/1#issuecomment-1",
            issue_url="https://api.github.com/repos/o/r/issues/1",
            author="user", kind="issue",
        )
        issue_ref = IssueRef(
            number=1, title="t", url="https://github.com/o/r/issues/1", labels=(),
        )
        ref = MentionTrigger(trigger_type="comment", comment=comment, issue=issue_ref)
        await impl.mention(monitor, ref, trigger_types=["comment"], mode="comment")

        assert len(client.calls) == 1
        # Para o mode "comment" de issue, o comportamento atual NÃO usa nowait.
        # Se este assertion mudar, documente por quê.
        call = client.calls[0]
        # O modo comment (follow_ups) não passa nowait — wait=True é esperado.
        assert call["wait"] is True, (
            "mention mode='comment' deve continuar usando wait=True "
            "(sem risco de hang equivalente ao PR-unified)."
        )


class TestMentionPrConcurrencyGuardPreserved:
    """CONCURRENT_DISPATCH_BLOCKED deve ser tratado identicamente ao wait=True."""

    async def test_concurrent_dispatch_blocked_returns_not_ok(self):
        """409 CONCURRENT_DISPATCH_BLOCKED deve retornar outcome com ok=False."""
        from deile.infrastructure.deile_worker_client import \
            WorkerDispatchError

        async def _dispatch_409(payload, *, wait, endpoint_url=None):
            raise WorkerDispatchError("blocked", error_code="CONCURRENT_DISPATCH_BLOCKED")

        client = MagicMock()
        client.dispatch = _dispatch_409
        impl, _ = _make_implementer(client=client)
        monitor, github = _make_monitor(implementer=impl)

        ref = _make_pr_mention_ref()
        outcome = await impl.mention(monitor, ref, trigger_types=["assignee"], mode="pr_unified")

        assert not outcome.ok
        # O guard retorna DISPATCH_SKIPPED_CONCURRENT (ou similar) — basta ok=False.
        assert outcome.error, "error deve estar preenchido quando bloqueado"


# ---------------------------------------------------------------------------
# Regressão #713: DISPATCH_SKIPPED_STILL_RUNNING não deve consumir tentativa
# ---------------------------------------------------------------------------

class TestMentionSkipStillRunningGuard:
    """Guard: DISPATCH_SKIPPED_STILL_RUNNING não consome tentativa de resume.

    Sem o guard, ``_dispatch_mention_group`` chamava ``update_from_worker``
    incondicionalmente — mesmo quando o dispatch foi *pulado* porque o
    claude-worker já estava rodando. Isso incrementava ``attempt`` +1 por tick
    até ``resume_max_attempts``, disparando ``_comment_mention_gave_up`` e
    abandonando uma PR saudável em progresso.
    """

    async def test_attempt_not_incremented_on_skip_still_running(self):
        """attempt NÃO deve mudar quando DISPATCH_SKIPPED_STILL_RUNNING é retornado.

        Sem o guard:
          attempt = resume_max - 1  → tick skip → attempt = resume_max
          → próximo tick: ceiling dispara → PR abandonada.
        Com o guard: attempt permanece em resume_max - 1 enquanto o worker vive.
        """
        from deile.orchestration.pipeline.stages import (
            MentionTrigger, _dispatch_mention_group,
        )
        from deile.orchestration.pipeline.labels import MENTION_DONE

        # _make_monitor sobrescreve impl.mention quando impl é MagicMock —
        # por isso configuramos o outcome DEPOIS de criar o monitor.
        monitor, github = _make_monitor()
        monitor.implementer.mention = AsyncMock(
            return_value=WorkOutcome(
                ok=False, text="",
                error="DISPATCH_SKIPPED_STILL_RUNNING: dispatch #123 still alive",
            )
        )

        pr_number = 99
        resume_max = monitor.config.resume_max_attempts  # default 10
        # Semeia attempt no limite − 1: sem o guard o próximo tick alcança o teto.
        state = monitor._resume_tracker.get(pr_number)
        state.attempt = resume_max - 1

        pr = _pr_ref(pr_number)
        group = [MentionTrigger(trigger_type="assignee", pr=pr)]
        dedup_key = f"pr:{pr_number}:assignee"

        await _dispatch_mention_group(monitor, dedup_key, group, "deile-one", 0.0)

        # (a) attempt NÃO deve ter sido incrementado.
        after = monitor._resume_tracker.get(pr_number).attempt
        assert after == resume_max - 1, (
            f"DISPATCH_SKIPPED_STILL_RUNNING deve preservar attempt; "
            f"esperado {resume_max - 1}, obtido {after}"
        )

        # (b) ~mention:processado NÃO deve ter sido aplicado.
        for call_args in github.add_labels.call_args_list:
            labels = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("labels", [])
            assert MENTION_DONE not in labels, (
                f"~mention:processado não deve ser aplicado em DISPATCH_SKIPPED_STILL_RUNNING; "
                f"encontrado em add_labels({call_args})"
            )

    async def test_gave_up_not_triggered_after_multiple_skips(self):
        """Múltiplos ticks de DISPATCH_SKIPPED_STILL_RUNNING não devem dar gave_up.

        Demonstra que o guard bloqueia a regressão: sem ele, após 2 ticks
        (attempt sobe de resume_max-1 para resume_max, depois dispara o ceiling)
        a PR seria abandonada. Com o guard, 2 ticks são inofensivos.
        """
        from deile.orchestration.pipeline.stages import (
            MentionTrigger, _dispatch_mention_group,
        )
        from deile.orchestration.pipeline.labels import MENTION_DONE

        monitor, github = _make_monitor()
        monitor.implementer.mention = AsyncMock(
            return_value=WorkOutcome(
                ok=False, text="",
                error="DISPATCH_SKIPPED_STILL_RUNNING: dispatch #456 still alive",
            )
        )

        pr_number = 100
        resume_max = monitor.config.resume_max_attempts
        state = monitor._resume_tracker.get(pr_number)
        state.attempt = resume_max - 1

        pr = _pr_ref(pr_number)
        group = [MentionTrigger(trigger_type="assignee", pr=pr)]
        dedup_key = f"pr:{pr_number}:assignee"

        # Dois ticks consecutivos de skip.
        await _dispatch_mention_group(monitor, dedup_key, group, "deile-one", 0.0)
        await _dispatch_mention_group(monitor, dedup_key, group, "deile-one", 0.0)

        # Attempt permanece inalterado após 2 ticks.
        after = monitor._resume_tracker.get(pr_number).attempt
        assert after == resume_max - 1, (
            f"Dois ticks de DISPATCH_SKIPPED_STILL_RUNNING não devem queimar o ceiling; "
            f"esperado {resume_max - 1}, obtido {after}"
        )

        # ~mention:processado nunca deve ter sido aplicado.
        for call_args in github.add_labels.call_args_list:
            labels = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("labels", [])
            assert MENTION_DONE not in labels, (
                "~mention:processado não deve ser aplicado em DISPATCH_SKIPPED_STILL_RUNNING"
            )
