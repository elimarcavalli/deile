"""Reconcile fire-and-forget (issue #373) — crítica / refino / pr_review.

Prova que o dispatch é não-bloqueante (só claima + grava task_id no ledger) e
que o VEREDITO é processado no reconcile do tick seguinte, lendo o resultado do
worker via ``/v1/dispatches/{task_id}/resume-info``. Cobre os três estados que
o reconcile precisa distinguir (rodando / concluído / sumido), os vereditos
(CLARO/VAGO, OK/waiting/convergência, merged/blocked/sem-merge), a concorrência
(``_count_total_in_flight`` ⇒ claima até ``available``) e o reaper estendido
para refino claimed-por-dispatch.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.implementer import WorkerImplementer
from deile.orchestration.pipeline.labels import (REFINAR, REVIEW_IN_PROGRESS,
                                                 REVIEW_PENDING,
                                                 WORKFLOW_ARCHITECTURE,
                                                 WORKFLOW_BLOCKED,
                                                 WORKFLOW_NEW,
                                                 WORKFLOW_REFINING,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)

_NOTIFIER_METHODS = (
    "issue_picked_up", "issue_reviewed", "implementation_started",
    "implementation_finished", "implementation_parked", "implementation_resumed",
    "implementation_blocked", "pr_picked_up", "pr_reviewed",
    "issue_auto_classified", "follow_ups_processed", "error",
    "pr_auto_classified", "mention_processed",
)


class _Client:
    """Worker fake fire-and-forget: dispatch nowait → task_id; reconcile lê
    resume-info. ``results`` mapeia task_id → payload de resume-info."""

    def __init__(self, *, verdict: str = "", ok: bool = True,
                 is_error: bool = False, running: bool = False,
                 gone: bool = False):
        self.payloads: List[dict] = []
        self._verdict = verdict
        self._ok = ok
        self._is_error = is_error
        self._running = running
        self._gone = gone
        self._seq = 0

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        if wait:
            return {"ok": self._ok, "summary": self._verdict}
        if not self._ok:
            from deile.infrastructure.deile_worker_client import \
                WorkerDispatchError
            raise WorkerDispatchError("rejected", error_code="WORKER_REJECTED")
        self._seq += 1
        return {"task_id": f"t-{self._seq:03d}", "status": "running"}

    async def get_resume_info(self, task_id, *, endpoint_url=None):
        if self._gone:
            from deile.infrastructure.deile_worker_client import \
                WorkerDispatchError
            raise WorkerDispatchError("gone", error_code="NOT_FOUND")
        if self._running:
            return {
                "last_completed_at": None, "last_is_error": None,
                "last_result_full": "", "last_result_summary": "",
                "claude_alive": True, "workdir_exists": True,
            }
        return {
            "last_completed_at": 1_700_000_000, "last_is_error": self._is_error,
            "last_result_full": self._verdict,
            "last_result_summary": self._verdict[:200],
            "claude_alive": False, "workdir_exists": True,
        }


def _issue(number: int, *labels: str, title: str = "t", body: str = "corpo",
           author: str = "alice") -> IssueRef:
    return IssueRef(
        number=number, title=title,
        url=f"https://github.com/owner/name/issues/{number}",
        labels=tuple(labels), body=body, state="open", author=author,
    )


def _ledger_path() -> Path:
    return Path(tempfile.mkdtemp(prefix=".test_ledger_")) / "dispatches.json"


def _make(client: _Client, *, label_map=None, prs=None, max_parallel=2,
          get_issue_body="corpo refinado", get_pr_ret="open"):
    lm = dict(label_map or {})
    registry: Dict[int, IssueRef] = {}
    for issues in lm.values():
        for i in issues:
            registry.setdefault(i.number, i)
    cfg = PipelineConfig(
        repo="owner/name", base_repo_path=Path("/tmp/fake"), notify_user_id="42",
        dispatch_mode="deile_worker", enable_refinement_gate=True,
        max_parallel=max_parallel, enable_resume=False, enable_classify=False,
        enable_pr_triage=False, enable_mention_handling=False,
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: list(lm.get(label, []))
    )
    github.list_open_prs = AsyncMock(return_value=list(prs or []))
    github.has_open_pr_for_issue = AsyncMock(return_value=False)
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.clear_batch_label = AsyncMock()
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.assign_issue = AsyncMock()
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)

    def _get_issue(number):
        base = registry.get(number)
        labels = base.labels if base is not None else ("feature",)
        author = base.author if base is not None else "alice"
        return _issue(number, *labels, author=author, body=get_issue_body)
    github.get_issue = AsyncMock(side_effect=_get_issue)

    async def _get_pr(number):
        return None if get_pr_ret is None else PrRef(
            number=number, title="t", url="u",
            labels=(REVIEW_IN_PROGRESS,), head_ref="auto/issue-1",
        )
    github.get_pr = AsyncMock(side_effect=_get_pr)

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    ledger = DispatchLedger(path=_ledger_path())
    monitor = PipelineMonitor(
        cfg, github=github, notifier=notifier,
        implementer=WorkerImplementer(client=client, ledger=ledger),
    )
    monitor._test_registry = registry
    return monitor, github, ledger


def _transitions(github):
    out = []
    for call in github.transition_issue.await_args_list:
        n = call.args[0] if call.args else call.kwargs.get("number")
        out.append((n, call.kwargs.get("from_label"), call.kwargs.get("to_label")))
    return out


def _pr_transitions(github):
    out = []
    for call in github.transition_pr.await_args_list:
        n = call.args[0] if call.args else call.kwargs.get("number")
        out.append((n, call.kwargs.get("from_label"), call.kwargs.get("to_label")))
    return out


# ===========================================================================
# Dispatch-side é NÃO-BLOQUEANTE (só claima + grava ledger)
# ===========================================================================

class TestDispatchIsNonBlocking:
    async def test_critique_only_claims_and_records_ledger(self):
        client = _Client(verdict="VEREDITO: CLARO")
        monitor, github, ledger = _make(
            client, label_map={WORKFLOW_NEW: [_issue(1, "feature")]},
        )
        await monitor._review_one_new_issue()
        # Claimou nova→em_revisao, mas NÃO aplicou veredito (sem revisada ainda).
        t = _transitions(github)
        assert (1, WORKFLOW_NEW, WORKFLOW_REVIEWING) in t
        assert (1, WORKFLOW_REVIEWING, WORKFLOW_REVIEWED) not in t
        # Gravou task_id no ledger.
        entry = ledger.get(DispatchLedger.key_for_issue(1))
        assert entry is not None and entry["task_id"]

    async def test_refine_records_before_body_in_ledger_extra(self):
        client = _Client(verdict="REFINO: OK")
        monitor, github, ledger = _make(
            client,
            label_map={
                REFINAR: [_issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="b0")],
                WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [],
            },
        )
        await monitor._refine_one_issue()
        entry = ledger.get(DispatchLedger.key_for_issue(6))
        assert entry is not None
        assert entry["extra"]["before_body"] == "b0"


# ===========================================================================
# reconcile_critique_issues
# ===========================================================================

class TestReconcileCritique:
    def _seed(self, monitor, github, number, *type_labels):
        own = monitor.identity.ownership_label()
        github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: (
                [_issue(number, WORKFLOW_REVIEWING, own)]
                if label == WORKFLOW_REVIEWING else []
            )
        )

    async def test_running_keeps_lock(self):
        client = _Client(verdict="VEREDITO: CLARO", running=True)
        monitor, github, _ = _make(client, label_map={WORKFLOW_NEW: [_issue(1, "feature")]})
        await monitor._review_one_new_issue()
        self._seed(monitor, github, 1)
        github.transition_issue.reset_mock()
        await monitor._reconcile_critique_issues()
        # Rodando → nenhuma transição (mantém em_revisao).
        assert _transitions(github) == []

    async def test_done_clear_goes_revisada(self):
        client = _Client(verdict="Analisei.\nVEREDITO: CLARO")
        monitor, github, ledger = _make(client, label_map={WORKFLOW_NEW: [_issue(1, "feature")]})
        await monitor._review_one_new_issue()
        self._seed(monitor, github, 1)
        await monitor._reconcile_critique_issues()
        assert (1, WORKFLOW_REVIEWING, WORKFLOW_REVIEWED) in _transitions(github)
        assert ledger.get(DispatchLedger.key_for_issue(1)) is None  # ledger limpo

    async def test_done_vago_goes_arquitetura(self):
        client = _Client(verdict="VEREDITO: VAGO: falta contrato")
        monitor, github, _ = _make(client, label_map={WORKFLOW_NEW: [_issue(2, "feature")]})
        await monitor._review_one_new_issue()
        self._seed(monitor, github, 2)
        await monitor._reconcile_critique_issues()
        assert (2, WORKFLOW_REVIEWING, WORKFLOW_ARCHITECTURE) in _transitions(github)

    async def test_gone_clears_ledger_no_transition(self):
        client = _Client(verdict="VEREDITO: CLARO", gone=True)
        monitor, github, ledger = _make(client, label_map={WORKFLOW_NEW: [_issue(1, "feature")]})
        await monitor._review_one_new_issue()
        self._seed(monitor, github, 1)
        github.transition_issue.reset_mock()
        await monitor._reconcile_critique_issues()
        # Sumida → não mexe no label, limpa o ledger (reaper cuida).
        assert _transitions(github) == []
        assert ledger.get(DispatchLedger.key_for_issue(1)) is None


# ===========================================================================
# reconcile_refine_issues
# ===========================================================================

class TestReconcileRefine:
    def _make_refine(self, client, body="corpo", get_issue_body="reescrito"):
        issue = _issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body=body)
        monitor, github, ledger = _make(
            client,
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            get_issue_body=get_issue_body,
        )
        return monitor, github, ledger, issue

    def _seed(self, monitor, github, issue):
        own = monitor.identity.ownership_label()
        seeded = _issue(issue.number, "feature", WORKFLOW_ARCHITECTURE, REFINAR, own)
        github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: (
                [seeded] if label == WORKFLOW_ARCHITECTURE else []
            )
        )

    async def test_ok_cosmetic_change_promotes_to_revisada(self):
        """Fix loop critic↔architect: REFINO:OK + mudança só COSMÉTICA (body
        quase igual, ≤2%) promove a revisada SEM re-crítica. Antes do fix a
        guarda byte-idêntico não disparava e gerava loop até o teto."""
        before = "x" * 200
        after = "x" * 201  # +0,5% → cosmético
        client = _Client(verdict="Só corrigi arquivo:linha.\nREFINO: OK")
        monitor, github, _, issue = self._make_refine(client, body=before, get_issue_body=after)
        await monitor._refine_one_issue()
        self._seed(monitor, github, issue)
        await monitor._reconcile_refine_issues()
        t = _transitions(github)
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_REVIEWED) in t
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) not in t

    async def test_ok_substantial_change_returns_to_nova(self):
        """REFINO:OK mas o passe mudou o body de forma substancial (>2%) → ainda
        re-critica (o refino está trabalhando, não convergiu)."""
        before = "x" * 100
        after = "y" * 400  # +300% → mudança real
        client = _Client(verdict="Reescrevi bastante.\nREFINO: OK")
        monitor, github, _, issue = self._make_refine(client, body=before, get_issue_body=after)
        await monitor._refine_one_issue()
        self._seed(monitor, github, issue)
        await monitor._reconcile_refine_issues()
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in _transitions(github)

    async def test_convergence_promotes_to_revisada(self):
        client = _Client(verdict="Pronto.\nREFINO: OK")
        # before_body (no ledger) == after_body (get_issue) ⇒ convergiu (idêntico).
        monitor, github, _, issue = self._make_refine(client, body="estavel", get_issue_body="estavel")
        await monitor._refine_one_issue()
        self._seed(monitor, github, issue)
        await monitor._reconcile_refine_issues()
        t = _transitions(github)
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_REVIEWED) in t
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) not in t

    async def test_unknown_body_changed_returns_to_nova(self):
        """Veredito ambíguo (unknown) só converge com body IDÊNTICO; mudança
        (mesmo cosmética) → re-crítica. Só REFINO:OK tolera cosmético."""
        before = "x" * 200
        after = "x" * 201  # cosmético, MAS verdict unknown → não tolera
        client = _Client(verdict="Mexi em algo mas não declarei veredito.")
        monitor, github, _, issue = self._make_refine(client, body=before, get_issue_body=after)
        await monitor._refine_one_issue()
        self._seed(monitor, github, issue)
        await monitor._reconcile_refine_issues()
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in _transitions(github)

    async def test_waiting_adds_aguardando_stakeholder(self):
        client = _Client(verdict="Sugestões postadas.\nREFINO: AGUARDA_STAKEHOLDER")
        monitor, github, _, issue = self._make_refine(client)
        await monitor._refine_one_issue()
        self._seed(monitor, github, issue)
        await monitor._reconcile_refine_issues()
        added = [
            lb for c in github.add_labels.await_args_list
            if c.args[1] == 6 for lb in c.args[2]
        ]
        from deile.orchestration.pipeline.labels import WORKFLOW_WAITING
        assert WORKFLOW_WAITING in added

    async def test_running_keeps_lock(self):
        client = _Client(verdict="REFINO: OK", running=True)
        monitor, github, _, issue = self._make_refine(client)
        await monitor._refine_one_issue()
        self._seed(monitor, github, issue)
        github.transition_issue.reset_mock()
        await monitor._reconcile_refine_issues()
        assert _transitions(github) == []


# ===========================================================================
# reconcile_review_prs (ground-truth)
# ===========================================================================

class TestReconcileReviewPrs:
    def _fresh_pr(self):
        return PrRef(number=10, title="prt", url="https://x/pull/10",
                     labels=(REVIEW_PENDING,), head_ref="auto/issue-1")

    def _seed_in_progress(self, monitor, github):
        in_progress = PrRef(number=10, title="prt", url="https://x/pull/10",
                            labels=(REVIEW_IN_PROGRESS,), head_ref="auto/issue-1")
        github.list_open_prs = AsyncMock(return_value=[in_progress])

    async def test_merged_goes_concluida(self):
        client = _Client(verdict="merged")
        monitor, github, ledger = _make(
            client, prs=[self._fresh_pr()], get_pr_ret=None,  # get_pr None = merged
        )
        monitor.config.enable_refinement_gate = False
        await monitor._review_one_open_pr()
        self._seed_in_progress(monitor, github)
        await monitor._reconcile_review_prs()
        from deile.orchestration.pipeline.labels import REVIEW_CONCLUDED
        assert (10, REVIEW_IN_PROGRESS, REVIEW_CONCLUDED) in _pr_transitions(github)
        assert monitor.notifier.pr_reviewed.await_count == 1
        _, kw = monitor.notifier.pr_reviewed.await_args
        assert kw.get("merged") is True

    async def test_done_no_merge_marks_concluida(self):
        client = _Client(verdict="revisei, deixei comentário")  # sem merge nem block
        monitor, github, _ = _make(client, prs=[self._fresh_pr()], get_pr_ret="open")
        monitor.config.enable_refinement_gate = False
        await monitor._review_one_open_pr()
        self._seed_in_progress(monitor, github)
        await monitor._reconcile_review_prs()
        from deile.orchestration.pipeline.labels import REVIEW_CONCLUDED
        assert (10, REVIEW_IN_PROGRESS, REVIEW_CONCLUDED) in _pr_transitions(github)
        _, kw = monitor.notifier.pr_reviewed.await_args
        assert kw.get("merged") is False

    async def test_blocked_marks_bloqueada(self):
        client = _Client(verdict="BLOQUEADO: CI vermelho")
        monitor, github, _ = _make(client, prs=[self._fresh_pr()], get_pr_ret="open")
        monitor.config.enable_refinement_gate = False
        await monitor._review_one_open_pr()
        self._seed_in_progress(monitor, github)
        await monitor._reconcile_review_prs()
        added = [
            lb for c in github.add_labels.await_args_list
            if c.args[1] == 10 for lb in c.args[2]
        ]
        assert WORKFLOW_BLOCKED in added

    async def test_running_keeps_em_andamento(self):
        client = _Client(verdict="merged", running=True)
        monitor, github, _ = _make(client, prs=[self._fresh_pr()], get_pr_ret=None)
        monitor.config.enable_refinement_gate = False
        await monitor._review_one_open_pr()
        self._seed_in_progress(monitor, github)
        github.transition_pr.reset_mock()
        await monitor._reconcile_review_prs()
        assert _pr_transitions(github) == []


# ===========================================================================
# Concorrência — _count_total_in_flight ⇒ claima até available
# ===========================================================================

class TestConcurrency:
    async def test_critique_dispatches_up_to_available(self):
        client = _Client(verdict="VEREDITO: CLARO")
        news = [_issue(n, "feature", title=f"t{n}") for n in (1, 2, 3, 4)]
        monitor, github, _ = _make(
            client, label_map={WORKFLOW_NEW: news}, max_parallel=3,
        )
        await monitor._review_one_new_issue()
        # Sem nada em voo: available = 3 ⇒ claima 3 (nova→em_revisao).
        claims = [t for t in _transitions(github)
                  if t[1] == WORKFLOW_NEW and t[2] == WORKFLOW_REVIEWING]
        assert len(claims) == 3
        assert len(client.payloads) == 3

    async def test_in_flight_reduces_available(self):
        client = _Client(verdict="VEREDITO: CLARO")
        news = [_issue(n, "feature", title=f"t{n}") for n in (1, 2, 3)]
        own = "~by:default"
        # Uma issue já em em_implementacao (1 slot ocupado).
        in_impl = _issue(99, "feature", "~workflow:em_implementacao", own)
        from deile.orchestration.pipeline.labels import WORKFLOW_IMPLEMENTING
        lm = {WORKFLOW_NEW: news, WORKFLOW_IMPLEMENTING: [in_impl]}
        monitor, github, _ = _make(client, label_map=lm, max_parallel=3)
        await monitor._review_one_new_issue()
        claims = [t for t in _transitions(github)
                  if t[1] == WORKFLOW_NEW and t[2] == WORKFLOW_REVIEWING]
        # available = 3 - 1 (em voo) = 2.
        assert len(claims) == 2

    async def test_aguardando_stakeholder_does_not_count_as_in_flight(self):
        """Regressão: issue parada em ``em_arquitetura`` + ``aguardando_stakeholder``
        espera o humano por tempo indefinido e NÃO consome slot de worker. Antes
        do fix, ``_count_total_in_flight`` só excluía bloqueada/em_pr, então um
        backlog de parked stakeholders fixava ``in_flight`` em ``max_parallel`` e
        esfomeava toda crítica nova (#515 ficou em nova por horas com in_flight=3
        = 1 órfã em_arquitetura + 2 aguardando_stakeholder)."""
        from deile.orchestration.pipeline.labels import (WORKFLOW_ARCHITECTURE,
                                                         WORKFLOW_WAITING)
        client = _Client(verdict="VEREDITO: CLARO")
        news = [_issue(n, "feature", title=f"t{n}") for n in (1, 2, 3)]
        own = "~by:default"
        # 3 issues paradas aguardando stakeholder (em_arquitetura) — NÃO contam.
        parked = [
            _issue(80 + k, "feature", WORKFLOW_ARCHITECTURE, WORKFLOW_WAITING, own)
            for k in range(3)
        ]
        lm = {WORKFLOW_NEW: news, WORKFLOW_ARCHITECTURE: parked}
        monitor, github, _ = _make(client, label_map=lm, max_parallel=3)
        await monitor._review_one_new_issue()
        claims = [t for t in _transitions(github)
                  if t[1] == WORKFLOW_NEW and t[2] == WORKFLOW_REVIEWING]
        # available = 3 - 0 (parados não contam) = 3 ⇒ todas as 3 novas claimadas.
        assert len(claims) == 3


# ===========================================================================
# Reaper estendido — refino claimed-por-dispatch (só com ledger entry)
# ===========================================================================

class TestReaperRefineExtended:
    async def test_reaps_refine_only_with_ledger_entry(self):
        import time

        from deile.orchestration.pipeline.stages import reap_orphan_claims
        client = _Client()
        own = "~by:default"
        # Issue em em_arquitetura há muito tempo, COM ledger entry (dispatch travado).
        issue = _issue(50, "feature", WORKFLOW_ARCHITECTURE, own)
        monitor, github, ledger = _make(
            client, label_map={WORKFLOW_ARCHITECTURE: [issue]},
        )
        monitor.config.reaper_stale_seconds = 60
        ledger.record(DispatchLedger.key_for_issue(50), task_id="stuck", session_id="")
        github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)
        await reap_orphan_claims(monitor)
        # Reapou: removeu em_arquitetura, recolocou nova.
        added = [
            lb for c in github.add_labels.await_args_list
            if c.args[1] == 50 for lb in c.args[2]
        ]
        assert WORKFLOW_NEW in added
        # Limpou o ledger (task abandonada).
        assert ledger.get(DispatchLedger.key_for_issue(50)) is None

    async def test_does_not_reap_refine_resting_without_ledger(self):
        import time

        from deile.orchestration.pipeline.stages import reap_orphan_claims
        client = _Client()
        own = "~by:default"
        # Issue em em_arquitetura há muito tempo, SEM ledger entry (descanso).
        issue = _issue(51, "feature", WORKFLOW_ARCHITECTURE, own)
        monitor, github, _ = _make(
            client, label_map={WORKFLOW_ARCHITECTURE: [issue]},
        )
        monitor.config.reaper_stale_seconds = 60
        github.label_applied_at = AsyncMock(return_value=int(time.time()) - 9999)
        await reap_orphan_claims(monitor)
        # NÃO reapou (sem ledger entry = descanso entre passes).
        added = [
            lb for c in github.add_labels.await_args_list
            if c.args[1] == 51 for lb in c.args[2]
        ]
        assert WORKFLOW_NEW not in added


# === Anti-loop same-tick (issue #418) =======================================
class TestRefineAntiLoopSameTick:
    """Regressão da causa-raiz do loop de #418. reconcile_refine_issues roda
    ANTES de refine_one_issue no mesmo tick; ao convergir, promove a issue a
    revisada e a marca em ``_refine_promoted_this_tick``. O índice de labels do
    GitHub tem eventual consistency, então a issue ainda reaparece sob
    ``refinar``/``em_arquitetura`` na listagem de refine_one_issue — sem o guard
    same-tick o rehydrate a rebaixaria de volta (revisada→em_arquitetura) → loop.
    """

    async def test_refine_skips_issue_promoted_this_tick(self):
        client = _Client(verdict="REFINO: OK")
        # Snapshot STALE: índice ainda lista #6 sob em_arquitetura + refinar.
        stale = _issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="b")
        monitor, github, ledger = _make(
            client,
            label_map={REFINAR: [stale], WORKFLOW_REFINING: [],
                       WORKFLOW_ARCHITECTURE: [stale]},
        )
        # Simula: reconcile já promoveu #6 a revisada NESTE tick.
        monitor._refine_promoted_this_tick.add(6)
        github.transition_issue.reset_mock()

        await monitor._refine_one_issue()

        # NÃO rebaixou (nenhuma transition) e NÃO re-dispatchou refino.
        assert _transitions(github) == []
        assert ledger.get(DispatchLedger.key_for_issue(6)) is None

    async def test_refine_proceeds_when_not_promoted(self):
        """Contraprova: set vazio → refino segue normal (fluxo não regrediu)."""
        client = _Client(verdict="REFINO: OK")
        stale = _issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="b")
        monitor, github, ledger = _make(
            client,
            label_map={REFINAR: [stale], WORKFLOW_REFINING: [],
                       WORKFLOW_ARCHITECTURE: [stale]},
        )
        assert 6 not in monitor._refine_promoted_this_tick
        await monitor._refine_one_issue()
        assert ledger.get(DispatchLedger.key_for_issue(6)) is not None

    async def test_convergence_marks_promoted_set(self):
        """A convergência (promoção a revisada) registra a issue no set same-tick."""
        client = _Client(verdict="Pronto.\nREFINO: OK")
        issue = _issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="estavel")
        monitor, github, _ = _make(
            client,
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [],
                       WORKFLOW_ARCHITECTURE: [issue]},
            get_issue_body="estavel",
        )
        await monitor._refine_one_issue()
        own = monitor.identity.ownership_label()
        seeded = _issue(6, "feature", WORKFLOW_ARCHITECTURE, REFINAR, own)
        github.list_issues_with_label = AsyncMock(
            side_effect=lambda label, **_: ([seeded] if label == WORKFLOW_ARCHITECTURE else [])
        )
        await monitor._reconcile_refine_issues()
        assert 6 in monitor._refine_promoted_this_tick
