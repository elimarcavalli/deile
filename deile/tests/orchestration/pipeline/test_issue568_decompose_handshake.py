"""Issue #568 — Decomposição via menção e via refine/architect: handshake de estado.

Verifica as duas garantias do fix:

1. ``_apply_refine_verdict`` detecta ``DECOMPOSTO:`` no output do architect e
   transiciona a issue para ``~workflow:decomposta`` (idempotência + liberação de
   slot de in_flight).

2. ``_dispatch_mention_group`` (caminho one-shot de menção) detecta ``DECOMPOSTO:``
   no outcome e aplica o handshake via ``_apply_decompose_handshake_from_mention``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from deile.orchestration.forge import GhCommandError
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef,
                                                        PrRef)
from deile.orchestration.pipeline.implementer import WorkOutcome, WorkerImplementer
from deile.orchestration.pipeline.labels import (
    MENTION_DONE, REFINAR, WORKFLOW_ARCHITECTURE, WORKFLOW_BLOCKED,
    WORKFLOW_DECOMPOSED, WORKFLOW_NEW, WORKFLOW_REFINING, WORKFLOW_REVIEWED,
    make_refine_attempt_label,
)
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOTIFIER_METHODS = (
    "issue_picked_up", "issue_reviewed", "implementation_started",
    "implementation_finished", "implementation_parked", "implementation_resumed",
    "implementation_blocked", "pr_picked_up", "pr_reviewed",
    "issue_auto_classified", "follow_ups_processed", "error",
    "pr_auto_classified", "mention_processed",
)


def _issue(number: int, *labels: str, body: str = "corpo", author: str = "alice") -> IssueRef:
    return IssueRef(
        number=number,
        title="t",
        url=f"https://github.com/owner/name/issues/{number}",
        labels=tuple(labels),
        body=body,
        state="open",
        author=author,
    )


def _comment(comment_id: int, body: str, *, author: str = "user1") -> CommentRef:
    return CommentRef(
        comment_id=comment_id,
        body=body,
        html_url=f"https://github.com/o/r/issues/1#issuecomment-{comment_id}",
        issue_url="https://api.github.com/repos/o/r/issues/1",
        author=author,
        kind="issue",
    )


def _transitions(github: MagicMock) -> List[Tuple[int, str, str]]:
    out = []
    for call_ in github.transition_issue.await_args_list:
        number = call_.args[0] if call_.args else call_.kwargs.get("number")
        out.append((number, call_.kwargs.get("from_label"), call_.kwargs.get("to_label")))
    return out


# ---------------------------------------------------------------------------
# Fix 1: _apply_refine_verdict detecta DECOMPOSTO no output do architect
# ---------------------------------------------------------------------------

class _SeqWorkerClient:
    """Worker client fake para testes fire-and-forget (espelha test_refinement_gate)."""

    def __init__(self, responses: List[dict]):
        self._responses = list(responses)
        self.payloads: List[dict] = []
        self.dispatched_tasks: List[str] = []
        self._task_results: Dict[str, dict] = {}
        self._seq = 0

    def _next_response(self) -> dict:
        return self._responses.pop(0) if self._responses else {"ok": True, "summary": ""}

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        resp = self._next_response()
        if wait:
            return resp
        if not resp.get("ok", True):
            from deile.infrastructure.deile_worker_client import WorkerDispatchError
            raise WorkerDispatchError(resp.get("error", "rejected"), error_code="WORKER_REJECTED")
        self._seq += 1
        task_id = f"task-{self._seq:04d}"
        self.dispatched_tasks.append(task_id)
        self._task_results[task_id] = {"ok": bool(resp.get("ok", True)), "summary": resp.get("summary", "")}
        return {"task_id": task_id, "status": "running"}

    async def get_resume_info(self, task_id, *, endpoint_url=None):
        result = self._task_results.get(task_id)
        if result is None:
            from deile.infrastructure.deile_worker_client import WorkerDispatchError
            raise WorkerDispatchError("not found", error_code="NOT_FOUND")
        return {
            "last_completed_at": 1_700_000_000,
            "last_is_error": not result["ok"],
            "last_result_full": result["summary"],
            "last_result_summary": result["summary"][:200],
            "claude_alive": False,
            "workdir_exists": True,
        }


_LEDGER_SEQ = [0]


def _new_ledger_path() -> Path:
    import tempfile
    _LEDGER_SEQ[0] += 1
    d = tempfile.mkdtemp(prefix=".test568_ledger_")
    return Path(d) / "dispatches.json"


def _make_refine_monitor(
    *,
    label_map: Optional[Dict[str, List[IssueRef]]] = None,
    worker_responses: Optional[List[dict]] = None,
) -> Tuple[PipelineMonitor, MagicMock, _SeqWorkerClient]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        dispatch_mode="deile_worker",
        enable_refinement_gate=True,
        max_parallel=2,
        refine_max_attempts=5,
        enable_resume=False,
        enable_classify=False,
        enable_pr_triage=False,
        enable_mention_handling=False,
    )
    lm = dict(label_map or {})
    registry: Dict[int, IssueRef] = {}
    for issues in lm.values():
        for i in issues:
            registry.setdefault(i.number, i)
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(side_effect=lambda label, **_: list(lm.get(label, [])))
    github.list_open_prs = AsyncMock(return_value=[])
    github.has_open_pr_for_issue = AsyncMock(return_value=False)
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.clear_batch_label = AsyncMock()
    github.transition_issue = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()

    def _get_issue(number):
        base = registry.get(number)
        labels = base.labels if base is not None else ("feature",)
        author = base.author if base is not None else "alice"
        return _issue(number, *labels, author=author, body="corpo refinado")
    github.get_issue = AsyncMock(side_effect=_get_issue)
    github.assign_issue = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    client = _SeqWorkerClient(worker_responses or [])
    from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
    ledger = DispatchLedger(path=_new_ledger_path())
    monitor = PipelineMonitor(
        cfg, github=github, notifier=notifier,
        implementer=WorkerImplementer(client=client, ledger=ledger),
    )
    monitor._test_issue_registry = registry
    return monitor, github, client


def _override_label(monitor, label: str, issues) -> None:
    prev = monitor.github.list_issues_with_label.side_effect

    async def _side(lbl, **kw):
        if lbl == label:
            return list(issues)
        res = prev(lbl, **kw)
        if hasattr(res, "__await__"):
            return await res
        return res

    monitor.github.list_issues_with_label = AsyncMock(side_effect=_side)


def _seed_refine_states_from_ledger(monitor, registry: dict) -> None:
    ledger = monitor.implementer._ledger
    arch = []
    for key in ledger.list_all():
        if not key.startswith("issue:"):
            continue
        number = int(key.split(":", 1)[1])
        base = registry.get(number)
        labels = list(base.labels) if base is not None else []
        own = monitor.identity.ownership_label()
        arch.append(_issue(number, *labels, REFINAR, own))
    _override_label(monitor, WORKFLOW_ARCHITECTURE, arch)


async def _refine_then_reconcile(monitor) -> None:
    await monitor._refine_one_issue()
    reg = getattr(monitor, "_test_issue_registry", {})
    _seed_refine_states_from_ledger(monitor, reg)
    await monitor._reconcile_refine_issues()


class TestRefineDetectsDecomposto:
    """Fix A: _apply_refine_verdict transiciona para WORKFLOW_DECOMPOSED quando o
    output do architect contém DECOMPOSTO: #n1 #n2..."""

    async def test_architect_output_decomposto_triggers_handshake(self):
        """Feature em em_arquitetura: architect cria derivadas → decomposta."""
        issue = _issue(100, "feature", WORKFLOW_ARCHITECTURE, REFINAR)
        monitor, github, _ = _make_refine_monitor(
            label_map={WORKFLOW_ARCHITECTURE: [issue], REFINAR: [issue]},
            worker_responses=[{"ok": True, "summary": "Criei as issues.\nDECOMPOSTO: #201 #202 #203"}],
        )
        await _refine_then_reconcile(monitor)
        t = _transitions(github)
        assert (100, WORKFLOW_ARCHITECTURE, WORKFLOW_DECOMPOSED) in t, (
            "feature em em_arquitetura com DECOMPOSTO: deve transicionar para decomposta"
        )

    async def test_architect_decomposto_cleans_refinar_label(self):
        """REFINAR e ~refine:N são removidos após o handshake de decomposição."""
        refine_label = make_refine_attempt_label(2)
        issue = _issue(101, "feature", WORKFLOW_ARCHITECTURE, REFINAR, refine_label)
        monitor, github, _ = _make_refine_monitor(
            label_map={WORKFLOW_ARCHITECTURE: [issue], REFINAR: [issue]},
            worker_responses=[{"ok": True, "summary": "DECOMPOSTO: #210 #211"}],
        )
        await _refine_then_reconcile(monitor)
        removed_calls = [call_.args[2] for call_ in github.remove_labels.await_args_list
                         if call_.args[1] == 101]
        all_removed = {lb for labels in removed_calls for lb in labels}
        assert REFINAR in all_removed, "REFINAR deve ser removido após decomposição"

    async def test_plain_refine_ok_still_transitions_normally(self):
        """REFINO: OK sem DECOMPOSTO segue o caminho normal (re-crítica ou nova)."""
        issue = _issue(102, "feature", WORKFLOW_ARCHITECTURE, REFINAR)
        monitor, github, _ = _make_refine_monitor(
            label_map={WORKFLOW_ARCHITECTURE: [issue], REFINAR: [issue]},
            worker_responses=[{"ok": True, "summary": "Refinei.\nREFINO: OK"}],
        )
        await _refine_then_reconcile(monitor)
        t = _transitions(github)
        assert (102, WORKFLOW_ARCHITECTURE, WORKFLOW_DECOMPOSED) not in t, (
            "REFINO: OK sem DECOMPOSTO não deve transicionar para decomposta"
        )

    async def test_intent_in_refining_with_decomposto_also_handled(self):
        """Intent em em_refinamento com DECOMPOSTO (edge case) também é tratado."""
        issue = _issue(103, "intent", WORKFLOW_REFINING, REFINAR)
        monitor, github, client = _make_refine_monitor(
            label_map={WORKFLOW_REFINING: [issue], REFINAR: [issue]},
            worker_responses=[{"ok": True, "summary": "DECOMPOSTO: #220 #221"}],
        )
        # Override: seed em_refinamento em vez de em_arquitetura
        ledger = monitor.implementer._ledger
        await monitor._refine_one_issue()
        refining = [_issue(103, "intent", WORKFLOW_REFINING, REFINAR, monitor.identity.ownership_label())]
        _override_label(monitor, WORKFLOW_REFINING, refining)
        _override_label(monitor, WORKFLOW_ARCHITECTURE, [])
        await monitor._reconcile_refine_issues()
        t = _transitions(github)
        assert (103, WORKFLOW_REFINING, WORKFLOW_DECOMPOSED) in t


# ---------------------------------------------------------------------------
# Fix 2: _dispatch_mention_group aplica handshake após one-shot com DECOMPOSTO
# ---------------------------------------------------------------------------

def _make_mention_monitor(
    *,
    mention_outcome_text: str = "done",
    mention_ok: bool = True,
    issue_labels: tuple = (),
) -> Tuple[PipelineMonitor, MagicMock]:
    """Monitor configurado para testes de dispatch de menção.

    O implementer_stub.mention retorna um WorkOutcome com o texto configurado.
    github.get_issue devolve a issue com ``issue_labels`` (usado pelo handshake
    para checar idempotência e descobrir o estado atual).
    """
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
    github.list_issue_comments_since = AsyncMock(
        return_value=[_comment(1, "@deile-one decomponha")]
    )
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.transition_issue = AsyncMock()
    # get_issue: devolve a issue com os labels fornecidos (para o handshake).
    github.get_issue = AsyncMock(
        return_value=IssueRef(
            number=1, title="t",
            url="https://github.com/owner/name/issues/1",
            labels=issue_labels,
        )
    )

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    implementer_stub = MagicMock()
    mention_outcome = WorkOutcome(
        ok=mention_ok,
        text=mention_outcome_text,
        error="" if mention_ok else "erro",
    )
    implementer_stub.mention = AsyncMock(return_value=mention_outcome)
    implementer_stub.implement = AsyncMock(return_value=WorkOutcome(ok=True, text=""))
    implementer_stub.review = AsyncMock(return_value=WorkOutcome(ok=True, text=""))

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=MagicMock(), notifier=notifier,
        implementer=implementer_stub,
    )
    return monitor, github


class TestMentionOneShot_DecomposeHandshake:
    """Fix B: _dispatch_mention_group aplica WORKFLOW_DECOMPOSED quando o
    outcome da menção one-shot contém DECOMPOSTO: #n1 #n2..."""

    async def test_mention_decomposto_output_triggers_handshake(self):
        """Menção one-shot em issue sem state ativo: DECOMPOSTO: → handshake."""
        monitor, github = _make_mention_monitor(
            mention_outcome_text="Criei as derivadas.\nDECOMPOSTO: #10 #11 #12",
            issue_labels=(),  # sem ~workflow:* → add_labels direto
        )
        await monitor._process_mentions()
        # Deve ter tentado aplicar WORKFLOW_DECOMPOSED
        added_calls = [
            list(c.args[2]) for c in github.add_labels.await_args_list
            if len(c.args) >= 3 and c.args[1] == 1
        ]
        all_added = {lb for labels in added_calls for lb in labels}
        assert WORKFLOW_DECOMPOSED in all_added, (
            "Menção com DECOMPOSTO: deve adicionar ~workflow:decomposta à issue pai"
        )

    async def test_mention_decomposto_with_refine_state_transitions(self):
        """Issue em em_refinamento (estado terminal não-gate): menção one-shot
        com DECOMPOSTO → transition a partir do estado de refino.

        Nota: em_arquitetura está em GATE_REDISPATCHES_COMMENT e é DEFERIDO para o
        refine stage (Fix A). Este teste cobre o caminho one-shot quando a issue está
        num estado que NÃO é re-despachado pela gate (sem ~workflow:* label),
        incluindo o caso em que o handshake rele get_issue e encontra REFINE_WORKFLOW_STATES.
        """
        # Simula: get_issue (para o handshake) retorna a issue com em_refinamento.
        # A menção one-shot foi gerada antes de a issue entrar no estado de refino.
        monitor, github = _make_mention_monitor(
            mention_outcome_text="DECOMPOSTO: #20 #21",
            issue_labels=(WORKFLOW_REFINING,),  # o que get_issue vai retornar
        )
        # Para que a menção NÃO seja deferida, o get_issue chamado pelo _dispatch_mention_group
        # deve retornar um estado que não está em GATE_REDISPATCHES_COMMENT.
        # Usamos side_effect: a primeira chamada (pelo router) retorna sem ~workflow:*;
        # a segunda (pelo handshake) retorna com em_refinamento.
        github.get_issue = AsyncMock(side_effect=[
            IssueRef(number=1, title="t", url="https://github.com/owner/name/issues/1", labels=()),
            IssueRef(number=1, title="t", url="https://github.com/owner/name/issues/1", labels=(WORKFLOW_REFINING,)),
        ])
        await monitor._process_mentions()
        t = _transitions(github)
        assert (1, WORKFLOW_REFINING, WORKFLOW_DECOMPOSED) in t, (
            "Issue em em_refinamento (relida no handshake) deve transicionar para decomposta"
        )

    async def test_mention_decomposto_idempotent_already_decomposed(self):
        """Issue já decomposta: handshake NÃO re-transiciona (idempotência)."""
        monitor, github = _make_mention_monitor(
            mention_outcome_text="DECOMPOSTO: #30 #31",
            issue_labels=(WORKFLOW_DECOMPOSED,),  # já decomposta
        )
        await monitor._process_mentions()
        t = _transitions(github)
        # Nenhuma transição deve ter sido feita (já está decomposta)
        decompose_transitions = [(n, f, to) for (n, f, to) in t if to == WORKFLOW_DECOMPOSED]
        assert decompose_transitions == [], (
            "Issue já decomposta não deve ser re-transicionada (idempotência)"
        )

    async def test_mention_no_decomposto_output_no_handshake(self):
        """Menção sem DECOMPOSTO: no output não dispara o handshake."""
        monitor, github = _make_mention_monitor(
            mention_outcome_text="Implementei a feature conforme solicitado.",
            issue_labels=(),
        )
        await monitor._process_mentions()
        added_calls = [
            list(c.args[2]) for c in github.add_labels.await_args_list
            if len(c.args) >= 3 and c.args[1] == 1
        ]
        all_added = {lb for labels in added_calls for lb in labels}
        assert WORKFLOW_DECOMPOSED not in all_added, (
            "Menção sem DECOMPOSTO: não deve adicionar ~workflow:decomposta"
        )

    async def test_mention_failed_outcome_no_handshake(self):
        """Menção com outcome ok=False não dispara o handshake mesmo com DECOMPOSTO."""
        monitor, github = _make_mention_monitor(
            mention_outcome_text="DECOMPOSTO: #40 #41",
            mention_ok=False,
            issue_labels=(),
        )
        await monitor._process_mentions()
        added_calls = [
            list(c.args[2]) for c in github.add_labels.await_args_list
            if len(c.args) >= 3 and c.args[1] == 1
        ]
        all_added = {lb for labels in added_calls for lb in labels}
        assert WORKFLOW_DECOMPOSED not in all_added, (
            "Outcome com ok=False não deve acionar o handshake de decomposição"
        )


# ---------------------------------------------------------------------------
# AC5: refine path — auto-referência NÃO aciona WORKFLOW_DECOMPOSED
# ---------------------------------------------------------------------------

class TestRefinePathRejectsAutoReference:
    """AC5: _apply_refine_verdict com parent_number correto — auto-ref não vira decomposta."""

    async def test_ac5_refine_autoref_does_not_trigger_decomposed(self):
        """Issue #768 em em_arquitetura; output contém REFINO: OK + auto-referência #768.
        Sem DECOMPOSTO: — deve seguir o caminho normal de refino, NÃO ir para decomposta.
        """
        issue = _issue(768, "feature", WORKFLOW_ARCHITECTURE, REFINAR)
        monitor, github, _ = _make_refine_monitor(
            label_map={WORKFLOW_ARCHITECTURE: [issue], REFINAR: [issue]},
            worker_responses=[{
                "ok": True,
                "summary": (
                    "Refinei a issue conforme solicitado.\n"
                    "REFINO: OK\n"
                    "Originada de #768 — ver histórico para detalhes."
                ),
            }],
        )
        await _refine_then_reconcile(monitor)
        t = _transitions(github)
        assert (768, WORKFLOW_ARCHITECTURE, WORKFLOW_DECOMPOSED) not in t, (
            "Auto-referência #768 nas últimas linhas NÃO deve disparar WORKFLOW_DECOMPOSED "
            f"(transições reais: {t})"
        )


# ---------------------------------------------------------------------------
# AC6: mention path — auto-referência NÃO adiciona WORKFLOW_DECOMPOSED
# ---------------------------------------------------------------------------

class TestMentionPathRejectsAutoReference:
    """AC6: _dispatch_mention_group com parent_number correto — auto-ref não vira decomposta."""

    async def test_ac6_mention_autoref_does_not_add_decomposed(self):
        """Mention one-shot sobre issue #1 (number=1); outcome menciona apenas #1.
        Sem DECOMPOSTO: — WORKFLOW_DECOMPOSED NÃO deve ser adicionado à issue.
        """
        monitor, github = _make_mention_monitor(
            mention_outcome_text=(
                "Analisei a issue #1 conforme solicitado.\n"
                "Ver #1 para o contexto completo."
            ),
            issue_labels=(),
        )
        await monitor._process_mentions()
        added_calls = [
            list(c.args[2]) for c in github.add_labels.await_args_list
            if len(c.args) >= 3 and c.args[1] == 1
        ]
        all_added = {lb for labels in added_calls for lb in labels}
        assert WORKFLOW_DECOMPOSED not in all_added, (
            "Auto-referência #1 no mention outcome NÃO deve adicionar WORKFLOW_DECOMPOSED "
            f"(labels adicionados: {all_added})"
        )
