"""Tests for the refinement gate + parallel decomposition (issue #257).

Exercises the worker-mode stage logic with mocked github / worker / notifier:

- CRITIQUE: CLARO → revisada (+ clears refinar); VAGO → refinar + the type's
  refine state (intent→em_refinamento, code→em_arquitetura); VAGO at the
  ceiling → block + assign author.
- REFINE: OK → bump count + back to nova; AGUARDA_STAKEHOLDER → waiting overlay;
  paused/blocked issues skipped; hand-applied ``refinar`` rehydrated.
- DECOMPOSE: a clear intent → ~workflow:decomposta (epic stays open).
- PARALLEL IMPLEMENT: up to ``max_parallel`` code issues dispatched together;
  ``intent`` excluded (it decomposes).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.forge import GhCommandError
from deile.orchestration.pipeline.github_client import IssueRef
from deile.orchestration.pipeline.implementer import WorkerImplementer
from deile.orchestration.pipeline.labels import (REFINAR,
                                                 WORKFLOW_ARCHITECTURE,
                                                 WORKFLOW_BLOCKED,
                                                 WORKFLOW_DECOMPOSED,
                                                 WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW, WORKFLOW_PR,
                                                 WORKFLOW_REFINING,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING,
                                                 WORKFLOW_WAITING,
                                                 make_refine_attempt_label)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)

_NOTIFIER_METHODS = (
    "issue_picked_up", "issue_reviewed", "implementation_started",
    "implementation_finished", "implementation_parked", "implementation_resumed",
    "implementation_blocked", "pr_picked_up", "pr_reviewed",
    "issue_auto_classified", "follow_ups_processed", "error",
    "pr_auto_classified", "mention_processed",
)


class _SeqWorkerClient:
    """Worker client fake compatível com o modelo fire-and-forget (issue #373).

    - ``dispatch(payload, wait=False)`` (crítica/refino nowait): consome a
      próxima resposta enfileirada, gera um ``task_id`` e MEMORIZA o veredito
      (``summary`` + flag ``ok``) associado a esse task_id. Devolve
      ``{"task_id": ..., "status": "running"}`` imediatamente.
    - ``dispatch(payload, wait=True)`` (decompose, resume bloqueante): devolve a
      resposta enfileirada direto (caminho legacy).
    - ``get_resume_info(task_id)`` (reconcile): devolve o resultado "concluído"
      do task — ``last_result_full`` carrega o veredito que o reconcile parseia.

    ``self.dispatched_tasks`` lista os task_ids despachados em ordem, pra os
    testes invocarem o reconcile e simularem o tick seguinte.
    """

    def __init__(self, responses: List[dict]):
        self._responses = list(responses)
        self.payloads: List[dict] = []
        self.dispatched_tasks: List[str] = []
        # task_id → {"ok", "summary", "is_error"} guardado no nowait.
        self._task_results: Dict[str, dict] = {}
        self._seq = 0
        # task_ids cuja sessão deve ser reportada como "ainda rodando".
        self.running_tasks: set = set()

    def _next_response(self) -> dict:
        if self._responses:
            return self._responses.pop(0)
        return {"ok": True, "summary": ""}

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        resp = self._next_response()
        if wait:
            return resp
        # nowait: o worker recusa o dispatch com erro tipado quando a resposta
        # enfileirada marca ``ok=False`` (espelha o worker devolvendo não-202).
        if not resp.get("ok", True):
            from deile.infrastructure.deile_worker_client import \
                WorkerDispatchError
            raise WorkerDispatchError(
                resp.get("error", "dispatch rejected"),
                error_code="WORKER_REJECTED",
            )
        # nowait: gera task_id, memoriza o veredito, devolve 202.
        self._seq += 1
        task_id = f"task-{self._seq:04d}"
        self.dispatched_tasks.append(task_id)
        self._task_results[task_id] = {
            "ok": bool(resp.get("ok", True)),
            "summary": resp.get("summary", ""),
        }
        return {"task_id": task_id, "status": "running"}

    async def get_resume_info(self, task_id, *, endpoint_url=None):
        result = self._task_results.get(task_id)
        if result is None:
            from deile.infrastructure.deile_worker_client import \
                WorkerDispatchError
            raise WorkerDispatchError("task not found", error_code="NOT_FOUND")
        if task_id in self.running_tasks:
            return {
                "last_completed_at": None,
                "last_is_error": None,
                "last_result_full": "",
                "last_result_summary": "",
                "claude_alive": True,
                "workdir_exists": True,
            }
        return {
            "last_completed_at": 1_700_000_000,
            "last_is_error": not result["ok"],
            "last_result_full": result["summary"],
            "last_result_summary": result["summary"][:200],
            "claude_alive": False,
            "workdir_exists": True,
        }


def _resp(summary: str, *, ok: bool = True) -> dict:
    return {"ok": ok, "summary": summary}


def _make_monitor(
    *,
    label_map: Optional[Dict[str, List[IssueRef]]] = None,
    worker_responses: Optional[List[dict]] = None,
    max_parallel: int = 2,
    refine_max_attempts: int = 5,
) -> Tuple[PipelineMonitor, MagicMock, _SeqWorkerClient]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        dispatch_mode="deile_worker",
        enable_refinement_gate=True,
        max_parallel=max_parallel,
        refine_max_attempts=refine_max_attempts,
        enable_resume=False,
        enable_classify=False,
        enable_pr_triage=False,
        enable_mention_handling=False,
    )
    lm = dict(label_map or {})
    # Registry número→IssueRef (preserva o type label) — usado pelo reconcile
    # quando relê o snapshot fresco via ``get_issue`` (issue #373).
    registry: Dict[int, IssueRef] = {}
    for _issues in lm.values():
        for _i in _issues:
            registry.setdefault(_i.number, _i)
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: list(lm.get(label, []))
    )
    github.list_open_prs = AsyncMock(return_value=[])
    github.has_open_pr_for_issue = AsyncMock(return_value=False)
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.clear_batch_label = AsyncMock()
    github.transition_issue = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    # get_issue é chamado por _persist_refine_attempt (lê labels), pelo guard de
    # convergência do refino e pelo reconcile de crítica (relê snapshot fresco).
    # Preserva os labels (type) do registry; body DIFERENTE do candidato (que usa
    # "corpo") por padrão = "o refino mudou o body" (caminho normal: volta pra
    # nova). Testes de convergência sobrescrevem get_issue p/ MESMO body.
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
    # Ledger isolado por monitor (issue #373): a crítica/refino fire-and-forget
    # grava task_id aqui e o reconcile lê. Sem path de tempfile o default
    # (~/.deile/pipeline/dispatches.json) vazaria entre testes.
    from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
    ledger = DispatchLedger(path=_new_ledger_path())
    monitor = PipelineMonitor(
        cfg, github=github, notifier=notifier,
        implementer=WorkerImplementer(client=client, ledger=ledger),
    )
    # Exposto pros helpers de reconcile (seeding por refine state).
    monitor._test_issue_registry = registry
    return monitor, notifier, client


_LEDGER_SEQ = [0]


def _new_ledger_path() -> Path:
    import tempfile
    _LEDGER_SEQ[0] += 1
    d = tempfile.mkdtemp(prefix=".test_ledger_")
    return Path(d) / "dispatches.json"


def _issue(number: int, *labels: str, title: str = "t", body: str = "corpo", author: str = "alice") -> IssueRef:
    return IssueRef(
        number=number, title=title, url=f"https://github.com/owner/name/issues/{number}",
        labels=tuple(labels), body=body, state="open", author=author,
    )


async def _critique_then_reconcile(monitor) -> None:
    """Despacha a crítica (fire-and-forget) e roda o reconcile no mesmo ato,
    simulando o tick-seguinte que processa o veredito (issue #373).

    Pra refletir o tick seguinte, a issue precisa estar listada em ``em_revisao``
    no reconcile. Reconfigura ``list_issues_with_label`` para devolver as issues
    travadas pelo dispatch quando o reconcile consultar ``~workflow:em_revisao``.
    """
    await monitor._review_one_new_issue()
    _seed_reviewing_from_transitions(monitor)
    await monitor._reconcile_critique_issues()


async def _refine_then_reconcile(monitor, *, registry=None) -> None:
    """Despacha o refino (fire-and-forget) e roda o reconcile (tick seguinte).

    Pro reconcile encontrar a issue, ela precisa estar listada no seu refine
    state (``em_refinamento``/``em_arquitetura``). Lê o ledger pra descobrir
    quais issues foram despachadas e as semeia no refine state correto.
    """
    await monitor._refine_one_issue()
    reg = registry if registry is not None else getattr(monitor, "_test_issue_registry", {})
    _seed_refine_states_from_ledger(monitor, reg)
    await monitor._reconcile_refine_issues()


def _seed_refine_states_from_ledger(monitor, registry: dict) -> None:
    """Semeia as listas ``em_refinamento``/``em_arquitetura`` com as issues que
    têm ledger entry (foram despachadas pelo refino)."""
    ledger = monitor.implementer._ledger
    refining, arch = [], []
    for key in ledger.list_all():
        if not key.startswith("issue:"):
            continue
        number = int(key.split(":", 1)[1])
        base = registry.get(number)
        labels = list(base.labels) if base is not None else []
        own = monitor.identity.ownership_label()
        if WORKFLOW_REFINING in labels:
            refining.append(_issue(number, *labels, REFINAR, own))
        else:
            arch.append(_issue(number, *labels, REFINAR, own))
    _override_label(monitor, WORKFLOW_REFINING, refining)
    _override_label(monitor, WORKFLOW_ARCHITECTURE, arch)


def _seed_reviewing_from_transitions(monitor) -> None:
    """Faz o forge fake listar em ``~workflow:em_revisao`` as issues que o
    dispatch acabou de travar (transição ``nova→em_revisao``), pro reconcile
    encontrá-las. Preserva o side_effect original pras demais labels."""
    reviewing = [
        n for (n, frm, to) in _transitions(monitor.github)
        if to == WORKFLOW_REVIEWING
    ]
    own = monitor.identity.ownership_label()
    issues = [_issue(n, WORKFLOW_REVIEWING, own) for n in reviewing]
    _override_label(monitor, WORKFLOW_REVIEWING, issues)


def _override_label(monitor, label: str, issues) -> None:
    """Sobrescreve o que ``list_issues_with_label`` devolve pra UMA label,
    mantendo o comportamento das demais."""
    prev = monitor.github.list_issues_with_label.side_effect

    async def _side(lbl, **kw):
        if lbl == label:
            return list(issues)
        res = prev(lbl, **kw)
        if hasattr(res, "__await__"):
            return await res
        return res

    monitor.github.list_issues_with_label = AsyncMock(side_effect=_side)


def _transitions(github: MagicMock) -> List[Tuple[int, str, str]]:
    """Flatten transition_issue calls to (number, from_label, to_label)."""
    out = []
    for call in github.transition_issue.await_args_list:
        number = call.args[0] if call.args else call.kwargs.get("number")
        out.append((number, call.kwargs.get("from_label"), call.kwargs.get("to_label")))
    return out


def _added(github: MagicMock, number: int) -> set:
    labels = set()
    for call in github.add_labels.await_args_list:
        if call.args[1] == number:
            labels.update(call.args[2])
    return labels


# ===========================================================================
# CRITIQUE
# ===========================================================================

class TestCritique:
    async def test_clear_goes_to_revisada_and_clears_refinar(self):
        # Fire-and-forget (issue #373): o dispatch só CLAIMA nova→em_revisao; o
        # veredito CLARO é aplicado no reconcile do tick seguinte.
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(1, "feature")]},
            worker_responses=[_resp("Analisei.\nVEREDITO: CLARO")],
        )
        await _critique_then_reconcile(monitor)
        t = _transitions(monitor.github)
        assert (1, WORKFLOW_NEW, WORKFLOW_REVIEWING) in t
        assert (1, WORKFLOW_REVIEWING, WORKFLOW_REVIEWED) in t
        # CLARO drops the refinar marker (+ any stale refine state, defensively).
        removed = [c.args[2] for c in monitor.github.remove_labels.await_args_list if c.args[1] == 1]
        assert any(REFINAR in lst for lst in removed)

    async def test_blocked_nova_issue_is_not_critiqued(self):
        """Hard-block: uma issue ``~workflow:nova`` que também carrega
        ``~workflow:bloqueada`` NÃO é critecada — sem transição nova→em_revisao,
        sem dispatch (gasto não-intencional). O label congela a issue em TODOS
        os estágios, não só no auto-resume."""
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(98, "feature", WORKFLOW_BLOCKED)]},
            worker_responses=[_resp("VEREDITO: CLARO")],
        )
        await monitor._review_one_new_issue()
        # Sem o filtro, a issue transicionaria nova→em_revisao (1º passo da
        # crítica). A ausência de QUALQUER transição para #98 prova que ela foi
        # excluída dos candidatos antes de qualquer processamento/dispatch.
        assert not any(n == 98 for (n, _f, _to) in _transitions(monitor.github))

    async def test_poor_feature_goes_to_arquitetura(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(2, "feature")]},
            worker_responses=[_resp("VEREDITO: VAGO: falta contrato")],
        )
        await _critique_then_reconcile(monitor)
        assert (2, WORKFLOW_REVIEWING, WORKFLOW_ARCHITECTURE) in _transitions(monitor.github)
        assert REFINAR in _added(monitor.github, 2)

    async def test_poor_intent_goes_to_refinamento(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(3, "intent")]},
            worker_responses=[_resp("VEREDITO: VAGO: template vazio")],
        )
        await _critique_then_reconcile(monitor)
        assert (3, WORKFLOW_REVIEWING, WORKFLOW_REFINING) in _transitions(monitor.github)

    async def test_poor_at_ceiling_blocks_and_assigns_author(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(4, "bug", author="bob")]},
            worker_responses=[_resp("VEREDITO: VAGO: sem repro")],
            refine_max_attempts=5,
        )
        monitor._resume_tracker.get(4).refine_attempt = 5  # ceiling already hit
        await _critique_then_reconcile(monitor)
        assert WORKFLOW_BLOCKED in _added(monitor.github, 4)
        monitor.github.assign_issue.assert_any_await(4, "bob")

    async def test_dispatch_failure_reverts_to_nova(self):
        # Falha de dispatch é detectada INLINE (o implementer devolve ok=False
        # imediatamente quando o worker recusa) — reverte em_revisao→nova no
        # próprio tick, sem precisar de reconcile.
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(5, "feature")]},
            worker_responses=[{"ok": False, "error": "boom"}],
        )
        await monitor._review_one_new_issue()
        assert (5, WORKFLOW_REVIEWING, WORKFLOW_NEW) in _transitions(monitor.github)


# ===========================================================================
# REFINE
# ===========================================================================

class TestRefine:
    async def test_ok_bumps_count_and_returns_to_nova(self):
        # Fire-and-forget (issue #373): o dispatch despacha; o veredito OK +
        # transição refine_state→nova são aplicados no reconcile (tick seguinte).
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [_issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE)]},
            worker_responses=[_resp("Reescrevi.\nREFINO: OK")],
        )
        await _refine_then_reconcile(monitor)
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in _transitions(monitor.github)
        assert monitor._resume_tracker.refine_attempt(6) == 1

    async def test_aguarda_stakeholder_pauses_with_overlay(self):
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [_issue(7, "intent", REFINAR, WORKFLOW_REFINING)]},
            worker_responses=[_resp("Postei sugestões.\nREFINO: AGUARDA_STAKEHOLDER")],
        )
        await _refine_then_reconcile(monitor)
        assert WORKFLOW_WAITING in _added(monitor.github, 7)
        # NOT returned to nova (paused).
        assert (7, WORKFLOW_REFINING, WORKFLOW_NEW) not in _transitions(monitor.github)

    async def test_waiting_issue_is_skipped(self):
        monitor, _, client = _make_monitor(
            label_map={REFINAR: [_issue(8, "intent", REFINAR, WORKFLOW_REFINING, WORKFLOW_WAITING)]},
            worker_responses=[_resp("REFINO: OK")],
        )
        await monitor._refine_one_issue()
        assert client.payloads == []  # paused → no dispatch

    async def test_hand_applied_refinar_is_rehydrated(self):
        # Human slapped ``refinar`` on a revisada issue → moved into refine state,
        # no dispatch this tick (refined on the next).
        monitor, _, client = _make_monitor(
            label_map={REFINAR: [_issue(9, "feature", REFINAR, WORKFLOW_REVIEWED)]},
        )
        await monitor._refine_one_issue()
        assert (9, WORKFLOW_REVIEWED, WORKFLOW_ARCHITECTURE) in _transitions(monitor.github)
        assert client.payloads == []


# ===========================================================================
# DECOMPOSE
# ===========================================================================

class TestDecompose:
    async def test_clear_intent_becomes_decomposed(self):
        intent = _issue(10, "intent", "~batch:abc12345")
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_REVIEWED: [intent]},
            worker_responses=[_resp("Criei.\nDECOMPOSTO: #21 #22")],
        )
        await monitor._decompose_one_reviewed_intent()
        assert (10, WORKFLOW_REVIEWED, WORKFLOW_DECOMPOSED) in _transitions(monitor.github)

    async def test_failure_without_derived_stays_revisada(self):
        intent = _issue(11, "intent", "~batch:abc12345")
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_REVIEWED: [intent]},
            worker_responses=[_resp("erro", ok=False)],
        )
        await monitor._decompose_one_reviewed_intent()
        assert (11, WORKFLOW_REVIEWED, WORKFLOW_DECOMPOSED) not in _transitions(monitor.github)


# ===========================================================================
# PARALLEL IMPLEMENT
# ===========================================================================

class TestParallelImplement:
    async def test_dispatches_up_to_max_parallel(self):
        reviewed = [_issue(n, "feature", "~batch:abc12345") for n in (30, 31, 32)]
        monitor, _, client = _make_monitor(
            label_map={WORKFLOW_REVIEWED: reviewed},
            worker_responses=[
                _resp("https://github.com/owner/name/pull/130"),
                _resp("https://github.com/owner/name/pull/131"),
            ],
            max_parallel=2,
        )
        await monitor._implement_one_reviewed_issue()
        claims = [t for t in _transitions(monitor.github)
                  if t[1] == WORKFLOW_REVIEWED and t[2] == WORKFLOW_IMPLEMENTING]
        assert len(claims) == 2  # capped at max_parallel
        assert len(client.payloads) == 2

    async def test_intent_is_excluded_from_implement(self):
        monitor, _, client = _make_monitor(
            label_map={WORKFLOW_REVIEWED: [_issue(40, "intent", "~batch:abc12345")]},
        )
        await monitor._implement_one_reviewed_issue()
        assert client.payloads == []  # intent is decomposed, not implemented

    async def test_skips_and_parks_when_open_pr_already_exists(self):
        # Dedup guard: a PR already implements #50 (e.g. via the mention path) →
        # do NOT open a second PR; park the issue in em_pr instead.
        monitor, _, client = _make_monitor(
            label_map={WORKFLOW_REVIEWED: [_issue(50, "feature", "~batch:abc12345")]},
        )
        monitor.github.has_open_pr_for_issue = AsyncMock(return_value=True)
        await monitor._implement_one_reviewed_issue()
        assert client.payloads == []  # no implementation dispatched
        assert (50, WORKFLOW_REVIEWED, WORKFLOW_PR) in _transitions(monitor.github)


class TestBriefSizeClamp:
    """A large (post-refine) body must never overflow the DispatchPayload.brief
    max_length — body truncation via ISSUE_BODY_MAX_CHARS must keep the brief
    under the wire-format cap."""

    async def test_critique_brief_never_exceeds_dispatch_cap(self):
        # Validate that the generated brief is accepted by DispatchPayload
        # (Pydantic raises on construction if the brief exceeds max_length).
        from deile.infrastructure.deile_worker_client import DispatchPayload
        huge = _issue(60, "feature", body="X" * 9000)
        monitor, _, client = _make_monitor(
            label_map={WORKFLOW_NEW: [huge]},
            worker_responses=[_resp("VEREDITO: CLARO")],
        )
        await monitor._review_one_new_issue()
        assert client.payloads, "critique must have dispatched"
        # If DispatchPayload was constructed without error, the brief is within bounds.
        brief_len = len(client.payloads[0]["brief"])
        assert brief_len > 0
        # Ensure it fits inside the current DispatchPayload.brief max_length (200_000).
        assert brief_len <= 200_000


# ===========================================================================
# SHARED REVIEWED SNAPSHOT (PR #380 follow-up — non-blocking review suggestion)
# ===========================================================================

def _reviewed_list_calls(github: MagicMock) -> int:
    """Count list_issues_with_label calls targeting ~workflow:revisada."""
    n = 0
    for call in github.list_issues_with_label.await_args_list:
        label = call.args[0] if call.args else call.kwargs.get("label")
        if label == WORKFLOW_REVIEWED:
            n += 1
    return n


class TestSharedReviewedSnapshot:
    """The implement + decompose stages share a single ~workflow:revisada fetch
    per tick (and a single ownership-ensure pass) instead of each issuing their
    own. Behavior is preserved via two views: implement filters the PRE-ensure
    snapshot (orphan code adopted next tick); decompose filters the POST-ensure
    snapshot (orphan intent decomposed same tick)."""

    async def test_reviewed_listed_once_per_dispatch(self):
        # A mix of intent + code so both stages have work to consider.
        reviewed = [
            _issue(80, "intent", "~batch:abc12345"),
            _issue(81, "feature", "~batch:abc12345"),
        ]
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_REVIEWED: reviewed},
            worker_responses=[_resp("DECOMPOSTO: #91"), _resp("https://github.com/owner/name/pull/191")],
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor._dispatch_stages()
        # Was 2 (implement + decompose each fetched independently); now 1 shared.
        assert _reviewed_list_calls(monitor.github) == 1

    async def test_orphan_intent_decomposed_same_tick(self):
        # No ~batch:, no ~by: — manually promoted to revisada. The default
        # monitor owns everything (shard 1), so it must adopt + decompose it.
        orphan = _issue(82, "intent")  # no batch, no ownership
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_REVIEWED: [orphan]},
            worker_responses=[_resp("DECOMPOSTO: #92 #93")],
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor._dispatch_stages()
        ownership = monitor.identity.ownership_label()
        assert ownership in _added(monitor.github, 82)  # adopted
        # Decomposed in the SAME tick (decompose filters the post-ensure view).
        assert (82, WORKFLOW_REVIEWED, WORKFLOW_DECOMPOSED) in _transitions(monitor.github)

    async def test_orphan_code_adopted_but_not_implemented_same_tick(self):
        # The implement path filters the PRE-ensure snapshot, so an orphan code
        # issue is labeled now but only claimed on the NEXT tick (preserving the
        # 1-tick latency the original two-fetch flow had).
        orphan = _issue(83, "feature")  # no batch, no ownership
        monitor, _, client = _make_monitor(
            label_map={WORKFLOW_REVIEWED: [orphan]},
        )
        monitor.config.enable_review = False
        monitor.config.enable_pr_review = False
        await monitor._dispatch_stages()
        ownership = monitor.identity.ownership_label()
        assert ownership in _added(monitor.github, 83)  # adopted (label added)
        # NOT claimed for implementation this tick.
        claims = [t for t in _transitions(monitor.github)
                  if t == (83, WORKFLOW_REVIEWED, WORKFLOW_IMPLEMENTING)]
        assert claims == []
        assert client.payloads == []

    async def test_ensure_ownership_label_returns_view_without_mutating_input(self):
        from deile.orchestration.pipeline.stages import _ensure_ownership_label
        orphan = _issue(84, "feature")            # gets adopted
        batched = _issue(85, "feature", "~batch:abc12345")  # no-op
        monitor, _, _ = _make_monitor()
        inp = [orphan, batched]
        out = await _ensure_ownership_label(monitor, inp)
        ownership = monitor.identity.ownership_label()
        # Input list untouched (pre-ensure view stays clean for implement).
        assert ownership not in inp[0].labels
        # Returned view reflects the added label for the orphan only.
        assert ownership in out[0].labels
        assert out[1] is batched  # batched issue reused as-is (no replace)

    async def test_forge_error_returns_none_pair(self):
        monitor, _, _ = _make_monitor()
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=GhCommandError(("gh", "issue", "list"), 1, "", "boom")
        )
        from deile.orchestration.pipeline.stages import \
            fetch_reviewed_and_ensure_ownership
        pre, post = await fetch_reviewed_and_ensure_ownership(monitor)
        assert pre is None and post is None


# ===========================================================================
# G2 — refine_one_issue seleciona por ESTADO (em_arquitetura / em_refinamento)
#      além de só pela label ``refinar``
# ===========================================================================

class TestRefineByState:
    """G2: seleção de candidatas une REFINAR + WORKFLOW_REFINING + WORKFLOW_ARCHITECTURE."""

    async def test_em_arquitetura_sem_refinar_selecionada_e_refinada(self):
        """Issue em ~workflow:em_arquitetura SEM o label ``refinar`` é
        selecionada por refine_one_issue e refinada (prova que a seleção por
        estado funciona — não depende do label ``refinar``)."""
        # A issue está em_arquitetura mas NÃO tem o label refinar.
        issue = _issue(200, "feature", WORKFLOW_ARCHITECTURE)
        monitor, _, client = _make_monitor(
            label_map={
                REFINAR: [],
                WORKFLOW_REFINING: [],
                WORKFLOW_ARCHITECTURE: [issue],
            },
            worker_responses=[_resp("Refinei.\nREFINO: OK")],
        )
        await _refine_then_reconcile(monitor)
        # Deve ter refinado (dispatch aconteceu).
        assert len(client.payloads) == 1
        # Volta para nova após OK (transição aplicada no reconcile).
        assert (200, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in _transitions(monitor.github)
        # Re-adicionou label refinar antes de refinar (passo idempotente).
        added = _added(monitor.github, 200)
        assert REFINAR in added

    async def test_em_refinamento_sem_refinar_selecionada_e_refinada(self):
        """Issue em ~workflow:em_refinamento (intent) SEM o label ``refinar``
        é selecionada e refinada."""
        issue = _issue(201, "intent", WORKFLOW_REFINING)
        monitor, _, client = _make_monitor(
            label_map={
                REFINAR: [],
                WORKFLOW_REFINING: [issue],
                WORKFLOW_ARCHITECTURE: [],
            },
            worker_responses=[_resp("Refinei.\nREFINO: OK")],
        )
        await _refine_then_reconcile(monitor)
        assert len(client.payloads) == 1
        assert (201, WORKFLOW_REFINING, WORKFLOW_NEW) in _transitions(monitor.github)
        assert REFINAR in _added(monitor.github, 201)

    async def test_refinar_sem_refine_state_ainda_funciona(self):
        """Issue com só ``refinar`` (sem refine state — humano aplicou à mão)
        continua funcionando: é rehydrated para o estado correto."""
        issue = _issue(202, "feature", REFINAR, WORKFLOW_REVIEWED)
        monitor, _, client = _make_monitor(
            label_map={
                REFINAR: [issue],
                WORKFLOW_REFINING: [],
                WORKFLOW_ARCHITECTURE: [],
            },
        )
        await monitor._refine_one_issue()
        # Rehydrate: move para em_arquitetura (feature), sem dispatch este tick.
        assert (202, WORKFLOW_REVIEWED, WORKFLOW_ARCHITECTURE) in _transitions(monitor.github)
        assert client.payloads == []

    async def test_dedup_issue_aparece_em_duas_listas_processada_uma_vez(self):
        """Se a mesma issue aparece em REFINAR e em WORKFLOW_ARCHITECTURE,
        é processada apenas uma vez."""
        issue = _issue(203, "feature", REFINAR, WORKFLOW_ARCHITECTURE)
        monitor, _, client = _make_monitor(
            label_map={
                REFINAR: [issue],
                WORKFLOW_REFINING: [],
                WORKFLOW_ARCHITECTURE: [issue],  # mesmo objeto, duplicado
            },
            worker_responses=[_resp("REFINO: OK")],
        )
        await monitor._refine_one_issue()
        # Apenas um dispatch, não dois.
        assert len(client.payloads) == 1

    async def test_forge_error_em_qualquer_lista_aborta_sem_crash(self):
        """Falha em qualquer das três chamadas list_issues_with_label aborta
        o stage com log, sem levantar exceção."""
        monitor, _, client = _make_monitor()
        monitor.github.list_issues_with_label = AsyncMock(
            side_effect=GhCommandError(("gh", "issue", "list"), 1, "", "boom")
        )
        # Não levanta — apenas retorna sem dispatch.
        await monitor._refine_one_issue()
        assert client.payloads == []


# ===========================================================================
# R1 — ~refine:N durável: persistência, reconciliação e limpeza
# ===========================================================================

class TestRefineAttemptDurable:
    """Prova que o contador de passes é persistido como label ~refine:N e
    reconciliado após restart (issue R1)."""

    async def test_ok_persiste_label_refine_apos_bump(self):
        """Após REFINO: OK, a label ~refine:1 deve ser gravada na issue."""
        issue = _issue(300, "feature", REFINAR, WORKFLOW_ARCHITECTURE)
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("REFINO: OK")],
        )
        await _refine_then_reconcile(monitor)
        # Verifica que add_labels foi chamado com ~refine:1 (no reconcile).
        added_all = [
            lb
            for call in monitor.github.add_labels.await_args_list
            if call.args[1] == 300
            for lb in call.args[2]
        ]
        assert make_refine_attempt_label(1) in added_all

    async def test_falha_dispatch_persiste_label_refine(self):
        """Dispatch falho também deve persistir ~refine:N (evita loop eterno)."""
        issue = _issue(301, "feature", REFINAR, WORKFLOW_ARCHITECTURE)
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("", ok=False)],
        )
        await monitor._refine_one_issue()
        added_all = [
            lb
            for call in monitor.github.add_labels.await_args_list
            if call.args[1] == 301
            for lb in call.args[2]
        ]
        assert make_refine_attempt_label(1) in added_all

    async def test_reconciliacao_com_label_duravel_apos_restart(self):
        """Issue com ~refine:5 (label durável, teto=5) deve bloquear imediatamente
        após restart, em vez de recomeçar do 0 (comportamento pré-R1).

        Sem a reconciliação: in-memory=0 < teto=5 → refina (custo extra indevido).
        Com a reconciliação: in-memory é elevado para 5 → 5 >= 5 → bloqueia.
        """
        # A issue já tem ~refine:5 gravada (sobreviveu ao restart).
        issue = _issue(302, "feature", REFINAR, WORKFLOW_ARCHITECTURE,
                       make_refine_attempt_label(5))
        monitor, _, client = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("REFINO: OK")],
            refine_max_attempts=5,
        )
        # In-memory começa zerado (simula restart).
        assert monitor._resume_tracker.refine_attempt(302) == 0
        await monitor._refine_one_issue()
        # Após reconciliação in-memory=5 >= teto=5, deve ter bloqueado.
        assert WORKFLOW_BLOCKED in _added(monitor.github, 302)
        # Nenhum dispatch deve ter ocorrido (teto atingido antes de refinar).
        assert client.payloads == []

    async def test_reconciliacao_nao_encolhe_contador(self):
        """set_refine_attempt nunca encolhe o contador in-memory."""
        issue = _issue(303, "feature", REFINAR, WORKFLOW_ARCHITECTURE,
                       make_refine_attempt_label(2))
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("REFINO: OK")],
            refine_max_attempts=5,
        )
        # In-memory já está em 3 (maior que a label 2).
        monitor._resume_tracker.get(303).refine_attempt = 3
        await _refine_then_reconcile(monitor)
        # Reconciliação com label=2 NÃO deve encolher o contador para 2.
        # Após bump(+1) no reconcile, deve ser 4 (não 3).
        assert monitor._resume_tracker.refine_attempt(303) == 4

    async def test_claro_remove_label_refine_na_critica(self):
        """Quando a crítica retorna CLARO, a label ~refine:N deve ser removida."""
        issue = _issue(304, "feature", REFINAR, make_refine_attempt_label(3))
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [issue]},
            worker_responses=[_resp("VEREDITO: CLARO")],
        )
        await _critique_then_reconcile(monitor)
        # Verifica que remove_labels foi chamado incluindo ~refine:3 (no reconcile)
        removed_all = [
            lb
            for call in monitor.github.remove_labels.await_args_list
            if call.args[1] == 304
            for lb in call.args[2]
        ]
        assert make_refine_attempt_label(3) in removed_all

    async def test_block_refinement_remove_label_refine(self):
        """_block_refinement deve remover ~refine:N para que o unblock recomece
        com contagem fresca."""
        issue = _issue(305, "bug", REFINAR, WORKFLOW_ARCHITECTURE,
                       make_refine_attempt_label(5), author="carol")
        monitor, _, client = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("REFINO: OK")],
            refine_max_attempts=5,
        )
        # In-memory já está no teto.
        monitor._resume_tracker.get(305).refine_attempt = 5
        await monitor._refine_one_issue()
        # Deve ter bloqueado (sem dispatch).
        assert WORKFLOW_BLOCKED in _added(monitor.github, 305)
        assert client.payloads == []
        # ~refine:5 deve ter sido removida.
        removed_all = [
            lb
            for call in monitor.github.remove_labels.await_args_list
            if call.args[1] == 305
            for lb in call.args[2]
        ]
        assert make_refine_attempt_label(5) in removed_all

    async def test_persist_best_effort_nao_derruba_stage(self):
        """Erro ao gravar ~refine:N (get_issue falha) não deve propagar — stage
        deve continuar e voltar a issue para nova."""
        issue = _issue(306, "feature", REFINAR, WORKFLOW_ARCHITECTURE)
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("REFINO: OK")],
        )
        # Simula get_issue falhando (rede down, etc.).
        monitor.github.get_issue = AsyncMock(side_effect=Exception("network error"))
        # Não deve levantar — stage continua normalmente (transição no reconcile).
        await _refine_then_reconcile(monitor, registry={306: issue})
        # Issue deve ter voltado a nova apesar do erro de persistência.
        assert (306, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in _transitions(monitor.github)


class TestRefineConvergence:
    """Guard de convergência (loop fix #438): um passe de refino que NÃO altera
    o body promove a issue a ``revisada`` em vez de re-circular para ``nova``."""

    async def test_body_inalterado_promove_a_revisada(self):
        issue = _issue(700, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="spec estável")
        monitor, _, client = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("Já está pronto.\nREFINO: OK")],
        )
        # O refino NÃO altera o body: get_issue devolve o MESMO body do candidato.
        monitor.github.get_issue = AsyncMock(
            side_effect=lambda number: _issue(
                number, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="spec estável"
            )
        )
        await _refine_then_reconcile(monitor, registry={700: issue})
        t = _transitions(monitor.github)
        # Convergiu → revisada (NÃO volta pra nova).
        assert (700, WORKFLOW_ARCHITECTURE, WORKFLOW_REVIEWED) in t
        assert (700, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) not in t
        # refinar removido (ciclo de refino encerrou).
        removed = [
            lb for c in monitor.github.remove_labels.await_args_list
            if c.args[1] == 700 for lb in c.args[2]
        ]
        assert REFINAR in removed
        # NÃO bumpou o contador — convergência não é um passe "gasto".
        assert monitor._resume_tracker.refine_attempt(700) == 0

    async def test_body_alterado_volta_para_nova(self):
        issue = _issue(701, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="spec v1")
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [issue], WORKFLOW_REFINING: [], WORKFLOW_ARCHITECTURE: [issue]},
            worker_responses=[_resp("Reescrevi.\nREFINO: OK")],
        )
        # O refino ALTERA o body → caminho normal (re-crítica).
        monitor.github.get_issue = AsyncMock(
            side_effect=lambda number: _issue(
                number, "feature", REFINAR, WORKFLOW_ARCHITECTURE, body="spec v2 reescrito"
            )
        )
        await _refine_then_reconcile(monitor, registry={701: issue})
        t = _transitions(monitor.github)
        assert (701, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in t
        assert (701, WORKFLOW_ARCHITECTURE, WORKFLOW_REVIEWED) not in t
        assert monitor._resume_tracker.refine_attempt(701) == 1
