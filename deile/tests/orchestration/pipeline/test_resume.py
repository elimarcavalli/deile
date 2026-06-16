"""Integration tests for the autonomous-pipeline resume feature (issue #254).

Exercises the pipeline-side stage logic end-to-end with mocked github / worker
/ notifier collaborators:

- fresh implement vs resume re-dispatch (RESUME mode, no reset);
- ground-truth end detection (concluido / incompleto / bloqueado);
- the progress guard (identical substantive fingerprint → block);
- attempt + budget ceilings → block flow;
- ``~workflow:bloqueada`` excludes from BOTH the implement queue and the
  auto-resume;
- block flow side effects (comment on issue + label + DM);
- resume applied to the review/merge stage too;
- the resume briefs (no ``git reset --hard``) and the structured-result parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.implementer import (
    WorkerImplementer,
    _outcome_from_worker_response,
)
from deile.orchestration.pipeline.labels import (
    REVIEW_CONCLUDED,
    REVIEW_IN_PROGRESS,
    REVIEW_PENDING,
    WORKFLOW_BLOCKED,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_PR,
    WORKFLOW_REVIEWED,
)
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor

_NOTIFIER_METHODS = (
    "issue_picked_up",
    "issue_reviewed",
    "implementation_started",
    "implementation_finished",
    "implementation_parked",
    "implementation_resumed",
    "implementation_blocked",
    "pr_picked_up",
    "pr_reviewed",
    "issue_auto_classified",
    "follow_ups_processed",
    "error",
    "pr_auto_classified",
    "mention_processed",
)


class _FakeWorkerClient:
    """Returns a queued sequence of worker responses (one per dispatch)."""

    def __init__(self, responses: List[dict]):
        self._responses = list(responses)
        self.payloads: List[dict] = []

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        if self._responses:
            return self._responses.pop(0)
        return {"ok": False, "summary": "no canned response"}


def _worker_response(
    *,
    ok: bool = True,
    summary: str = "",
    ended: str = "",
    pr_url: str = "",
    motivo_bloqueio: str = "",
    fingerprint: str = "",
    tentativa: int = 0,
    motivo_fim_loop: str = "natural",
    budget_acumulado_s: float = 0.0,
) -> dict:
    """Build a structured worker dispatch response (with the ``resume`` block)."""
    resp: dict = {"ok": ok, "summary": summary}
    resp["resume"] = {
        "ended": ended,
        "pr_url": pr_url,
        "motivo_bloqueio": motivo_bloqueio,
        "motivo_fim_loop": motivo_fim_loop,
        "fingerprint": fingerprint,
        "tentativa": tentativa,
        "budget_acumulado_s": budget_acumulado_s,
    }
    return resp


async def _drain_bg(monitor) -> None:
    """Aguarda as background tasks de resume terminarem (issue #445).

    O caminho de RESUME de review agora roda detached via ``spawn_background``;
    os testes precisam drenar para observar os efeitos (merge/block/notify).
    """
    import asyncio

    if monitor._bg_tasks:
        await asyncio.gather(*list(monitor._bg_tasks))


def _make_monitor(
    *,
    issues_reviewed: Optional[List[IssueRef]] = None,
    issues_in_progress: Optional[List[IssueRef]] = None,
    prs: Optional[List[PrRef]] = None,
    worker_responses: Optional[List[dict]] = None,
    enable_resume: bool = True,
    resume_max_attempts: int = 10,
    resume_budget: int = 0,
    resume_interval: int = 0,
) -> Tuple[PipelineMonitor, MagicMock, _FakeWorkerClient]:
    """Build a worker-mode monitor with only the resume/implement stages live."""
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        dispatch_mode="deile_worker",
        enable_resume=enable_resume,
        resume_max_attempts=resume_max_attempts,
        resume_budget=resume_budget,
        resume_interval=resume_interval,
        # Focus: keep only implement + resume + pr_review on.
        enable_classify=False,
        enable_review=False,
        enable_pr_triage=False,
        enable_mention_handling=False,
    )
    label_map: Dict[str, List[IssueRef]] = {
        WORKFLOW_REVIEWED: list(issues_reviewed or []),
        WORKFLOW_IMPLEMENTING: list(issues_in_progress or []),
    }
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: list(label_map.get(label, []))
    )
    github.list_open_prs = AsyncMock(return_value=list(prs or []))
    github.has_open_pr_for_issue = AsyncMock(return_value=False)
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.clear_batch_label = AsyncMock()
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    client = _FakeWorkerClient(worker_responses or [])
    implementer = WorkerImplementer(client=client)

    monitor = PipelineMonitor(
        cfg,
        github=github,
        notifier=notifier,
        implementer=implementer,
    )
    return monitor, notifier, client


# ===========================================================================
# Implementer-level: resume briefs (no reset) + structured-result parsing
# ===========================================================================


class TestResumeBriefs:
    async def test_resume_implement_brief_has_no_reset(self):
        client = _FakeWorkerClient(
            [_worker_response(ended="incompleto", fingerprint="f1")]
        )
        impl = WorkerImplementer(client=client)
        monitor = MagicMock()
        monitor.config = MagicMock(repo="owner/name", main_branch="main")
        monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
        issue = MagicMock(number=1, title="t", body="b")
        await impl.implement(monitor, issue, resume=True)
        brief = client.payloads[0]["brief"]
        # The fresh-start reset (``reset --hard origin/main``) must be ABSENT;
        # the brief may still PROHIBIT reset ("NÃO rode git reset --hard").
        assert "reset --hard origin/" not in brief
        assert "NÃO rode `git reset --hard`" in brief
        assert "RETOMADA" in brief
        assert ".deile-progress.md" in brief
        assert "git diff" in brief
        # The resume wire block tells the worker this is a resume.
        assert client.payloads[0]["resume"]["mode"] == "resume"

    async def test_fresh_implement_brief_keeps_reset(self):
        client = _FakeWorkerClient(
            [
                _worker_response(
                    ended="concluido", pr_url="https://github.com/owner/name/pull/1"
                )
            ]
        )
        impl = WorkerImplementer(client=client)
        monitor = MagicMock()
        monitor.config = MagicMock(repo="owner/name", main_branch="main")
        monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
        issue = MagicMock(number=1, title="t", body="b")
        await impl.implement(monitor, issue, resume=False)
        brief = client.payloads[0]["brief"]
        assert "reset --hard origin/main" in brief
        assert client.payloads[0]["resume"]["mode"] == "fresh"

    async def test_resume_review_brief_has_no_reset(self):
        # Após o refactor "PR é o quadro" o brief unificado de PR substitui o
        # brief de review_resume. Resume é coberto pelo PASSO 0 que instrui
        # ler ``.deile-progress.md`` (não há mais um cabeçalho "RETOMADA"
        # distinto — o mesmo brief atende fresh e resume).
        client = _FakeWorkerClient([_worker_response(ended="incompleto")])
        impl = WorkerImplementer(client=client)
        monitor = MagicMock()
        monitor.config = MagicMock(
            repo="owner/name",
            main_branch="main",
            mention_handle="@deile-one",
        )
        pr = MagicMock(
            number=7,
            title="t",
            head_ref="auto/issue-1",
            url="https://github.com/owner/name/pull/7",
        )
        await impl.review(monitor, pr, resume=True)
        brief = client.payloads[0]["brief"]
        # O brief unificado nunca emite ``git reset --hard`` (não fala em fresh
        # nem aqui nem no caminho não-resume — o worker trabalha em cima do
        # PVC já checked-out).
        assert "reset --hard origin/" not in brief
        # Resume é gratuito: o PASSO 0 lê ``.deile-progress.md`` se existir.
        assert ".deile-progress.md" in brief
        assert client.payloads[0]["resume"]["expect_merge"] is True


class TestStructuredResultParser:
    def test_parses_resume_block(self):
        out = _outcome_from_worker_response(
            _worker_response(
                ended="bloqueado",
                motivo_bloqueio="falta cred",
                fingerprint="fp",
                tentativa=4,
            )
        )
        assert out.ended == "bloqueado"
        assert out.motivo_bloqueio == "falta cred"
        assert out.fingerprint == "fp"
        assert out.tentativa == 4

    def test_legacy_response_without_resume_block(self):
        out = _outcome_from_worker_response({"ok": True, "summary": "https://x/pull/1"})
        assert out.ok is True
        assert out.ended == ""
        assert out.tentativa == 0

    def test_non_dict_is_failure(self):
        out = _outcome_from_worker_response("oops")
        assert out.ok is False


# ===========================================================================
# Stage 2: fresh implement, ground-truth driven
# ===========================================================================


def _reviewed(number=2):
    return IssueRef(
        number=number,
        title="impl me",
        url="u",
        labels=(WORKFLOW_REVIEWED, "~batch:abc12345"),
    )


def _in_progress(number=2, *, blocked=False):
    labels = (WORKFLOW_IMPLEMENTING, "~batch:abc12345")
    if blocked:
        labels = labels + (WORKFLOW_BLOCKED,)
    return IssueRef(number=number, title="continue me", url="u", labels=labels)


class TestFreshImplementGroundTruth:
    # Issue #373: fresh implements are now fire-and-forget (nowait=True).
    # The ground-truth handling (concluido / bloqueado / incompleto) is
    # done by reconcile_implementing_issues on subsequent ticks, NOT inline.
    # These tests verify the fire-and-forget dispatch behavior.

    async def test_concluido_moves_to_em_pr(self):
        # Issue #373: fire-and-forget dispatch. The worker response is NOT
        # parsed for structured outcome; reconcile handles PR detection.
        monitor, notifier, _ = _make_monitor(
            issues_reviewed=[_reviewed()],
            worker_responses=[
                _worker_response(
                    ended="concluido",
                    pr_url="https://github.com/owner/name/pull/3",
                    fingerprint="f1",
                    tentativa=1,
                )
            ],
        )
        await monitor.tick()
        notifier.implementation_started.assert_called_once()
        # Fire-and-forget: implementation_finished fires on reconcile, not here.
        notifier.implementation_finished.assert_not_called()
        assert monitor.stats.issues_implemented == 0
        # Only the claim transition (revisada → em_implementacao).
        # em_pr transition happens in reconcile.
        for call in monitor.github.transition_issue.call_args_list:
            assert call.kwargs.get("to_label") != WORKFLOW_PR

    async def test_bloqueado_triggers_block_flow(self):
        # Issue #373: fire-and-forget dispatch — block flow is NOT triggered
        # inline. The worker's bloqueado verdict would be surfaced by the
        # resume path (which still blocks and parses structured outcome) or
        # by reconcile via reaper on stale issues.
        monitor, notifier, _ = _make_monitor(
            issues_reviewed=[_reviewed()],
            worker_responses=[
                _worker_response(
                    ended="bloqueado",
                    motivo_bloqueio="falta a credencial X",
                    fingerprint="f1",
                    tentativa=1,
                )
            ],
        )
        await monitor.tick()
        # Fire-and-forget: block flow not triggered inline.
        # The issue is claimed and dispatched; reconcile/reaper will handle it.
        notifier.implementation_started.assert_called_once()
        notifier.implementation_blocked.assert_not_called()
        assert monitor.stats.issues_blocked == 0
        # Only the claim transition — no em_pr.
        for call in monitor.github.transition_issue.call_args_list:
            assert call.kwargs.get("to_label") != WORKFLOW_PR

    async def test_incompleto_parks_quietly_when_resume_enabled(self):
        # Issue #373: fire-and-forget dispatch — the worker response is not
        # parsed for structured outcome. The fingerprint is NOT absorbed
        # because _finalize_implement_outcome is not called.
        # The reconcile stage handles ground truth on subsequent ticks.
        monitor, notifier, _ = _make_monitor(
            issues_reviewed=[_reviewed()],
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="f1",
                    tentativa=1,
                )
            ],
        )
        await monitor.tick()
        # Fire-and-forget: no parking, no block, no finished.
        notifier.implementation_parked.assert_not_called()
        notifier.implementation_blocked.assert_not_called()
        notifier.implementation_finished.assert_not_called()
        # Fingerprint NOT absorbed — the fire-and-forget path skips
        # _finalize_implement_outcome which does the absorption.
        assert monitor._resume_tracker.get(2).last_fingerprint == ""

    async def test_incompleto_parks_with_dm_when_resume_disabled(self):
        # Issue #373: fire-and-forget dispatch — parking is handled by
        # reconcile/reaper, not inline. Even with resume disabled,
        # implementation_parked is NOT called on the dispatch tick.
        monitor, notifier, _ = _make_monitor(
            issues_reviewed=[_reviewed()],
            enable_resume=False,
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="f1",
                    tentativa=1,
                )
            ],
        )
        await monitor.tick()
        # Fire-and-forget: no parking notification on dispatch tick.
        notifier.implementation_started.assert_called_once()
        notifier.implementation_parked.assert_not_called()
        notifier.implementation_finished.assert_not_called()
        notifier.implementation_blocked.assert_not_called()
        assert monitor.stats.issues_implemented == 0

    async def test_blocked_issue_excluded_from_implement_queue(self):
        # An issue carrying ~workflow:bloqueada (even with a stale revisada
        # label) must NOT be re-dispatched by the implement stage.
        blocked = IssueRef(
            number=2,
            title="t",
            url="u",
            labels=(WORKFLOW_REVIEWED, WORKFLOW_BLOCKED, "~batch:abc12345"),
        )
        monitor, notifier, _ = _make_monitor(issues_reviewed=[blocked])
        await monitor.tick()
        notifier.implementation_started.assert_not_called()


# ===========================================================================
# Stage 2b: resume sweep
# ===========================================================================


class TestResumeSweep:
    async def test_resume_redispatches_in_resume_mode(self):
        monitor, notifier, client = _make_monitor(
            issues_in_progress=[_in_progress()],
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="f2",
                    tentativa=2,
                )
            ],
        )
        await monitor.tick()
        await _drain_bg(monitor)
        notifier.implementation_resumed.assert_called_once()
        assert monitor.stats.resume_dispatches == 1
        # The dispatch carried resume mode.
        assert client.payloads[-1]["resume"]["mode"] == "resume"

    async def test_resume_concludes_to_em_pr(self):
        monitor, notifier, _ = _make_monitor(
            issues_in_progress=[_in_progress()],
            worker_responses=[
                _worker_response(
                    ended="concluido",
                    pr_url="https://github.com/owner/name/pull/9",
                    fingerprint="f2",
                    tentativa=2,
                )
            ],
        )
        await monitor.tick()
        await _drain_bg(monitor)
        notifier.implementation_finished.assert_called_once()
        assert monitor.stats.issues_implemented == 1
        monitor.github.transition_issue.assert_any_call(
            2, from_label=WORKFLOW_IMPLEMENTING, to_label=WORKFLOW_PR
        )

    async def test_blocked_issue_excluded_from_resume(self):
        monitor, notifier, _ = _make_monitor(
            issues_in_progress=[_in_progress(blocked=True)],
            worker_responses=[_worker_response(ended="incompleto")],
        )
        await monitor.tick()
        notifier.implementation_resumed.assert_not_called()
        assert monitor.stats.resume_dispatches == 0

    async def test_resume_skips_when_open_pr_exists(self):
        """Ground-truth guard (anti-double-dispatch): se já existe PR aberta
        para a issue, o resume NÃO re-despacha — o worker já concluiu. Isola o
        guard de :func:`resume_in_progress_issues` chamando-o direto (sem o
        reconcile, que noutro caminho já a teria promovido a em_pr)."""
        from deile.orchestration.pipeline import stages

        monitor, notifier, client = _make_monitor(
            issues_in_progress=[_in_progress()],
            worker_responses=[_worker_response(ended="incompleto")],
        )
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        await stages.resume_in_progress_issues(monitor)
        await _drain_bg(monitor)
        notifier.implementation_resumed.assert_not_called()
        assert monitor.stats.resume_dispatches == 0
        assert client.payloads == []  # nenhum dispatch saiu

    async def test_resume_disabled_skips_sweep(self):
        monitor, notifier, _ = _make_monitor(
            issues_in_progress=[_in_progress()],
            enable_resume=False,
            worker_responses=[_worker_response(ended="incompleto")],
        )
        await monitor.tick()
        notifier.implementation_resumed.assert_not_called()
        assert monitor.stats.resume_dispatches == 0

    async def test_progress_guard_blocks_on_identical_fingerprint(self):
        # Prime the tracker with a fingerprint, then the resume returns the SAME
        # one → 0 progress → block flow.
        monitor, notifier, _ = _make_monitor(
            issues_in_progress=[_in_progress()],
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="SAME",
                    tentativa=2,
                )
            ],
        )
        monitor._resume_tracker.update_from_worker(
            2, fingerprint="SAME", attempt=1, budget_s=0.0
        )
        await monitor.tick()
        await _drain_bg(monitor)
        notifier.implementation_blocked.assert_called_once()
        monitor.github.add_labels.assert_any_call("issue", 2, [WORKFLOW_BLOCKED])
        assert monitor.stats.issues_blocked == 1

    async def test_progress_guard_continues_on_changed_fingerprint(self):
        monitor, notifier, _ = _make_monitor(
            issues_in_progress=[_in_progress()],
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="NEW",
                    tentativa=2,
                )
            ],
        )
        monitor._resume_tracker.update_from_worker(
            2, fingerprint="OLD", attempt=1, budget_s=0.0
        )
        await monitor.tick()
        await _drain_bg(monitor)
        notifier.implementation_blocked.assert_not_called()
        assert monitor._resume_tracker.get(2).last_fingerprint == "NEW"

    async def test_attempt_ceiling_triggers_block(self):
        monitor, notifier, client = _make_monitor(
            issues_in_progress=[_in_progress()],
            resume_max_attempts=3,
            worker_responses=[_worker_response(ended="incompleto")],
        )
        # Tracker already at the ceiling → block before dispatching.
        monitor._resume_tracker.update_from_worker(
            2, fingerprint="f", attempt=3, budget_s=0.0
        )
        await monitor.tick()
        notifier.implementation_blocked.assert_called_once()
        assert monitor.stats.issues_blocked == 1
        # No worker dispatch happened (blocked before).
        assert client.payloads == []

    async def test_budget_ceiling_triggers_block(self):
        monitor, notifier, client = _make_monitor(
            issues_in_progress=[_in_progress()],
            resume_budget=600,
            worker_responses=[_worker_response(ended="incompleto")],
        )
        monitor._resume_tracker.update_from_worker(
            2, fingerprint="f", attempt=1, budget_s=700.0
        )
        await monitor.tick()
        notifier.implementation_blocked.assert_called_once()
        assert client.payloads == []

    async def test_budget_accumulates_from_worker_then_blocks_next_tick(self):
        # End-to-end budget: a dispatch returns budget_acumulado_s over the
        # ceiling; the NEXT tick blocks on it (no pre-seeded tracker).
        ip = _in_progress()
        monitor, notifier, _ = _make_monitor(
            issues_in_progress=[ip],
            resume_budget=600,
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="f1",
                    tentativa=2,
                    budget_acumulado_s=900.0,
                ),
            ],
        )
        await monitor.tick()  # absorbs budget=900 from the worker result
        await _drain_bg(monitor)
        assert monitor._resume_tracker.get(2).budget_s == 900.0
        notifier.implementation_blocked.assert_not_called()
        await monitor.tick()  # now the budget ceiling fires
        notifier.implementation_blocked.assert_called_once()

    async def test_resume_implement_nao_bloqueia_o_tick(self):
        # Issue #445: o resume de implement roda detached via spawn_background —
        # resume_in_progress_issues retorna ANTES de o dispatch (lento) terminar.
        import asyncio

        from deile.orchestration.pipeline import stages

        monitor, notifier, _ = _make_monitor(issues_in_progress=[_in_progress()])
        gate = asyncio.Event()

        async def _slow_implement(_monitor, _issue, *, resume):
            await gate.wait()
            return _outcome_from_worker_response(
                _worker_response(ended="incompleto", fingerprint="f2", tentativa=2)
            )

        monitor.implementer = MagicMock()
        monitor.implementer.implement = AsyncMock(side_effect=_slow_implement)

        # O stage retorna imediatamente (dispatch ainda travado no gate).
        await stages.resume_in_progress_issues(monitor)
        assert len(monitor._bg_tasks) == 1
        assert 2 in monitor._resume_in_flight

        # Libera o dispatch e drena — o in-flight é limpo no finally.
        gate.set()
        await _drain_bg(monitor)
        assert 2 not in monitor._resume_in_flight

    async def test_cadence_skips_when_too_soon(self):
        monitor, notifier, client = _make_monitor(
            issues_in_progress=[_in_progress()],
            resume_interval=60,
            worker_responses=[_worker_response(ended="incompleto")],
        )
        # Stamp a very recent dispatch using the same clock the stage reads.
        import deile.orchestration.pipeline.stages as stages_mod

        now = stages_mod._monotonic()
        monitor._resume_tracker.record_dispatch(2, now)
        await monitor.tick()
        # Within the interval → not re-dispatched this tick.
        notifier.implementation_resumed.assert_not_called()
        assert client.payloads == []


# ===========================================================================
# Block flow side effects
# ===========================================================================


class TestBlockFlowSideEffects:
    # Issue #373: fire-and-forget dispatch — block flow is NOT triggered
    # inline on fresh dispatches. The bloqueado verdict is only surfaced
    # by the resume path (resume_in_progress_issues, which still blocks).

    async def test_block_comments_the_real_reason(self):
        # Issue #373: fire-and-forget — block flow not triggered inline.
        # The issue is claimed and dispatched; bloqueado verdict is ignored.
        monitor, notifier, _ = _make_monitor(
            issues_reviewed=[_reviewed()],
            worker_responses=[
                _worker_response(
                    ended="bloqueado",
                    motivo_bloqueio="precisa de revisão humana de segurança",
                )
            ],
        )
        await monitor.tick()
        # Fire-and-forget: no comment, no block.
        notifier.implementation_started.assert_called_once()
        notifier.implementation_blocked.assert_not_called()
        monitor.github.comment_on_issue.assert_not_called()

    async def test_block_keeps_em_implementacao_label(self):
        # Issue #373: fire-and-forget dispatch — the issue is claimed and
        # stays in em_implementacao. No block flow triggered inline.
        monitor, notifier, _ = _make_monitor(
            issues_reviewed=[_reviewed()],
            worker_responses=[_worker_response(ended="bloqueado", motivo_bloqueio="x")],
        )
        await monitor.tick()
        # The claim transition placed it in em_implementacao.
        # No block label added (reconcile handles it on subsequent ticks).
        notifier.implementation_started.assert_called_once()
        # No transition removed em_implementacao.
        for call in monitor.github.transition_issue.call_args_list:
            assert call.kwargs.get(
                "from_label"
            ) != WORKFLOW_IMPLEMENTING or call.kwargs.get("to_label") in (None,)
        # remove_labels never called on em_implementacao.
        for call in monitor.github.remove_labels.call_args_list:
            assert WORKFLOW_IMPLEMENTING not in (
                call.args[2] if len(call.args) > 2 else []
            )


# ===========================================================================
# Stage 3: resume on review/merge
# ===========================================================================


class TestReviewResume:
    async def test_fresh_review_is_nowait_then_resume_concludes(self):
        # Issue #373: review fresh agora é fire-and-forget (espelha implement).
        # Tick 1: PR em REVIEW_PENDING → fresh dispatch nowait → PR fica em
        #         em_andamento; pr_reviewed NÃO chamada ainda.
        # Tick 2: PR em REVIEW_IN_PROGRESS → resume bloqueante → pr_reviewed.
        pr_pending = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-2",
        )
        pr_in_progress = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_IN_PROGRESS,),
            head_ref="auto/issue-2",
        )

        monitor, notifier, client = _make_monitor(
            prs=[pr_pending],
            worker_responses=[
                # Tick 1 (nowait): worker aceita e retorna 202 + task_id imediato.
                {"task_id": "rev-t1", "status": "running"},
                # Tick 2 (resume bloqueante): worker termina e retorna resultado.
                _worker_response(
                    ended="concluido",
                    pr_url="https://x/pull/10",
                    summary="https://x/pull/10 MERGED",
                    fingerprint="f",
                    tentativa=2,
                ),
            ],
        )

        # Tick 1: fresh dispatch → fire-and-forget. pr_reviewed NÃO chamada.
        await monitor.tick()
        notifier.pr_reviewed.assert_not_called()
        assert monitor.stats.prs_reviewed == 0
        # O dispatch fire-and-forget foi feito (payload enfileirado no fake).
        assert len(client.payloads) == 1
        assert client.payloads[0]["resume"]["mode"] == "fresh"

        # Reconfigura a lista de PRs para o segundo tick (PR agora em_andamento).
        monitor.github.list_open_prs = AsyncMock(return_value=[pr_in_progress])

        # Tick 2: resume → concluido → pr_reviewed chamada (resume roda em bg).
        await monitor.tick()
        await _drain_bg(monitor)
        notifier.pr_reviewed.assert_called_once()
        _, kwargs = notifier.pr_reviewed.call_args
        assert kwargs.get("merged") is True
        assert monitor.stats.prs_reviewed == 1

    async def test_incomplete_review_stays_in_progress_for_resume(self):
        # A non-merged review with resume enabled keeps the PR in
        # ~review:em_andamento (NOT concluded) so the next tick resumes it.
        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_PENDING,),
            head_ref="auto/issue-2",
        )
        monitor, notifier, _ = _make_monitor(
            prs=[pr],
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    fingerprint="f1",
                    tentativa=1,
                )
            ],
        )
        await monitor.tick()
        # Never transitioned to concluded.
        for call in monitor.github.transition_pr.call_args_list:
            assert call.kwargs.get("to_label") != REVIEW_CONCLUDED

    async def test_in_progress_pr_is_resumed_in_resume_mode(self):
        # A PR already in ~review:em_andamento is a resume candidate.
        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_IN_PROGRESS,),
            head_ref="auto/issue-2",
        )
        monitor, notifier, client = _make_monitor(
            prs=[pr],
            worker_responses=[
                _worker_response(
                    ended="concluido",
                    summary="https://x/pull/10 MERGED",
                    pr_url="https://x/pull/10",
                    fingerprint="f2",
                    tentativa=2,
                )
            ],
        )
        await monitor.tick()
        await _drain_bg(monitor)
        notifier.implementation_resumed.assert_called_once()
        assert client.payloads[-1]["resume"]["mode"] == "resume"
        assert monitor.stats.prs_reviewed == 1

    async def test_blocked_pr_excluded_from_review_resume(self):
        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_IN_PROGRESS, WORKFLOW_BLOCKED),
            head_ref="auto/issue-2",
        )
        monitor, notifier, client = _make_monitor(
            prs=[pr],
            worker_responses=[_worker_response(ended="incompleto")],
        )
        await monitor.tick()
        notifier.implementation_resumed.assert_not_called()
        assert client.payloads == []

    async def test_review_block_on_agent_declaration(self):
        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_IN_PROGRESS,),
            head_ref="auto/issue-2",
        )
        monitor, notifier, _ = _make_monitor(
            prs=[pr],
            worker_responses=[
                _worker_response(
                    ended="bloqueado",
                    motivo_bloqueio="conflito irreconciliável",
                )
            ],
        )
        await monitor.tick()
        await _drain_bg(monitor)
        monitor.github.comment_on_pr.assert_called_once()
        monitor.github.add_labels.assert_any_call("pr", 10, [WORKFLOW_BLOCKED])
        notifier.implementation_blocked.assert_called_once()


# ============================================================================
# WORKER_AUTH_EXPIRED — estratégia C da issue #309 fase 3 (resiliência auth)
# ============================================================================


class TestOutcomePreservesErrorCode:
    """``_outcome_from_worker_response`` propaga o ``error_code`` retornado
    pelo claude_worker_server prefixando o ``error`` com ``[CODE]``. O
    monitor usa ``_classify_outcome_error`` pra detectar e tomar ação
    específica (bloquear PR/issue em auth-expired, etc)."""

    def test_outcome_prefixes_error_with_error_code_when_present(self):
        response = {
            "ok": False,
            "error_code": "WORKER_AUTH_EXPIRED",
            "error": "claude reportou token expirado",
            "summary": "",
        }
        outcome = _outcome_from_worker_response(response)
        assert outcome.ok is False
        assert outcome.error.startswith("[WORKER_AUTH_EXPIRED] ")
        assert "token expirado" in outcome.error

    def test_outcome_without_error_code_keeps_error_plain(self):
        """Backward compat: response sem ``error_code`` continua produzindo
        ``outcome.error`` sem prefixo (não muda comportamento legacy)."""
        response = {"ok": False, "error": "worker reported failure"}
        outcome = _outcome_from_worker_response(response)
        assert outcome.ok is False
        assert outcome.error == "worker reported failure"
        assert not outcome.error.startswith("[")

    def test_outcome_ok_response_ignores_error_code(self):
        """error_code só faz sentido em failure. response ok=True ignora."""
        response = {"ok": True, "summary": "review concluído"}
        outcome = _outcome_from_worker_response(response)
        assert outcome.ok is True
        assert outcome.error == ""


# ===========================================================================
# Regressão #509: skip-because-still-running NÃO consome tentativa
# ===========================================================================


class TestSkipStillRunningDoesNotBurnAttempt:
    """Um dispatch pulado porque o anterior AINDA roda no worker não é uma
    tentativa real — nenhum trabalho novo de review/merge aconteceu no tick.
    Antes do fix, ``_absorb_progress`` (chamado incondicionalmente) bumpava o
    contador +1 a cada skip; uma review saudável que durasse mais ticks que o
    teto (``resolve_stage_max_retries`` = 3) queimava todo o orçamento em
    skips e era bloqueada (#509: PR CLEAN+MERGEABLE → "teto 4/4 sem mergear").
    """

    @staticmethod
    def _skip_outcome():
        from deile.orchestration.pipeline.implementer import WorkOutcome

        return WorkOutcome(
            ok=False,
            text="",
            error=(
                "DISPATCH_SKIPPED_STILL_RUNNING: claude-worker ainda "
                "rodando o task anterior; skip nesse tick"
            ),
        )

    async def test_pr_review_skip_does_not_increment_attempt_nor_block(self):
        from deile.orchestration.pipeline import stages

        pr = PrRef(
            number=10,
            title="prt",
            url="https://x/pull/10",
            labels=(REVIEW_IN_PROGRESS,),
            head_ref="auto/issue-2",
        )
        monitor, notifier, _ = _make_monitor(prs=[pr])
        monitor.github.branch_exists = AsyncMock(return_value=True)
        # Implementer devolve skip (review anterior ainda viva).
        monitor.implementer = MagicMock()
        monitor.implementer.review = AsyncMock(return_value=self._skip_outcome())
        # 1 tentativa já registrada (bem abaixo do teto de 3).
        monitor._resume_tracker.update_from_worker(
            10, fingerprint="f", attempt=1, budget_s=0.0
        )
        before = monitor._resume_tracker.get(10).attempt

        await stages.review_one_open_pr(monitor)
        await _drain_bg(monitor)

        after = monitor._resume_tracker.get(10).attempt
        assert after == before == 1, "skip-still-running NÃO pode bumpar attempt"
        # NÃO bloqueou.
        notifier.implementation_blocked.assert_not_called()
        for call in monitor.github.add_labels.call_args_list:
            labels_arg = call.args[2] if len(call.args) > 2 else []
            assert WORKFLOW_BLOCKED not in labels_arg
        # batch transitório liberado (em_andamento permanece como lock durável).
        monitor.github.clear_batch_label.assert_awaited()

    async def test_implement_resume_skip_does_not_increment_attempt_nor_block(self):
        from deile.orchestration.pipeline import stages

        monitor, notifier, _ = _make_monitor(issues_in_progress=[_in_progress()])
        monitor._resume_tracker.update_from_worker(
            2, fingerprint="f", attempt=1, budget_s=0.0
        )
        before = monitor._resume_tracker.get(2).attempt

        await stages._finalize_implement_outcome(
            monitor,
            2,
            self._skip_outcome(),
            resume=True,
        )

        after = monitor._resume_tracker.get(2).attempt
        assert after == before == 1, "skip-still-running NÃO pode bumpar attempt"
        notifier.implementation_blocked.assert_not_called()
