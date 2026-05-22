"""Tests for the refinement gate + parallel decomposition (issue #257).

Exercises the worker-mode stage logic with mocked github / worker / notifier:

- CRITIQUE: CLARO → revisada (+ clears refinar); POBRE → refinar + the type's
  refine state (intent→em_refinamento, code→em_arquitetura); POBRE at the
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
                                                 WORKFLOW_WAITING)
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
    """Returns canned worker responses (one per dispatch); records payloads."""

    def __init__(self, responses: List[dict]):
        self._responses = list(responses)
        self.payloads: List[dict] = []

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        if self._responses:
            return self._responses.pop(0)
        return {"ok": True, "summary": ""}


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
    github.assign_issue = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    client = _SeqWorkerClient(worker_responses or [])
    monitor = PipelineMonitor(
        cfg, github=github, notifier=notifier, implementer=WorkerImplementer(client=client),
    )
    return monitor, notifier, client


def _issue(number: int, *labels: str, title: str = "t", body: str = "corpo", author: str = "alice") -> IssueRef:
    return IssueRef(
        number=number, title=title, url=f"https://github.com/owner/name/issues/{number}",
        labels=tuple(labels), body=body, state="open", author=author,
    )


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
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(1, "feature")]},
            worker_responses=[_resp("Analisei.\nVEREDITO: CLARO")],
        )
        await monitor._review_one_new_issue()
        t = _transitions(monitor.github)
        assert (1, WORKFLOW_NEW, WORKFLOW_REVIEWING) in t
        assert (1, WORKFLOW_REVIEWING, WORKFLOW_REVIEWED) in t
        # CLARO drops the refinar marker (+ any stale refine state, defensively).
        removed = [c.args[2] for c in monitor.github.remove_labels.await_args_list if c.args[1] == 1]
        assert any(REFINAR in lst for lst in removed)

    async def test_poor_feature_goes_to_arquitetura(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(2, "feature")]},
            worker_responses=[_resp("VEREDITO: POBRE: falta contrato")],
        )
        await monitor._review_one_new_issue()
        assert (2, WORKFLOW_REVIEWING, WORKFLOW_ARCHITECTURE) in _transitions(monitor.github)
        assert REFINAR in _added(monitor.github, 2)

    async def test_poor_intent_goes_to_refinamento(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(3, "intent")]},
            worker_responses=[_resp("VEREDITO: POBRE: template vazio")],
        )
        await monitor._review_one_new_issue()
        assert (3, WORKFLOW_REVIEWING, WORKFLOW_REFINING) in _transitions(monitor.github)

    async def test_poor_at_ceiling_blocks_and_assigns_author(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(4, "bug", author="bob")]},
            worker_responses=[_resp("VEREDITO: POBRE: sem repro")],
            refine_max_attempts=5,
        )
        monitor._resume_tracker.get(4).refine_attempt = 5  # ceiling already hit
        await monitor._review_one_new_issue()
        assert WORKFLOW_BLOCKED in _added(monitor.github, 4)
        monitor.github.assign_issue.assert_any_await(4, "bob")

    async def test_dispatch_failure_reverts_to_nova(self):
        monitor, _, _ = _make_monitor(
            label_map={WORKFLOW_NEW: [_issue(5, "feature")]},
            worker_responses=[_resp("", ok=False)],
        )
        await monitor._review_one_new_issue()
        assert (5, WORKFLOW_REVIEWING, WORKFLOW_NEW) in _transitions(monitor.github)


# ===========================================================================
# REFINE
# ===========================================================================

class TestRefine:
    async def test_ok_bumps_count_and_returns_to_nova(self):
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [_issue(6, "feature", REFINAR, WORKFLOW_ARCHITECTURE)]},
            worker_responses=[_resp("Reescrevi.\nREFINO: OK")],
        )
        await monitor._refine_one_issue()
        assert (6, WORKFLOW_ARCHITECTURE, WORKFLOW_NEW) in _transitions(monitor.github)
        assert monitor._resume_tracker.refine_attempt(6) == 1

    async def test_aguarda_stakeholder_pauses_with_overlay(self):
        monitor, _, _ = _make_monitor(
            label_map={REFINAR: [_issue(7, "intent", REFINAR, WORKFLOW_REFINING)]},
            worker_responses=[_resp("Postei sugestões.\nREFINO: AGUARDA_STAKEHOLDER")],
        )
        await monitor._refine_one_issue()
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
    """Issue #257: a large (post-refine) body must never overflow the 8000-char
    dispatch cap — the body sits last in the brief, so it is safely clamped."""

    async def test_critique_brief_never_exceeds_8000(self):
        huge = _issue(60, "feature", body="X" * 9000)  # nova: no batch (critique needs batch_id None)
        monitor, _, client = _make_monitor(
            label_map={WORKFLOW_NEW: [huge]},
            worker_responses=[_resp("VEREDITO: CLARO")],
        )
        await monitor._review_one_new_issue()
        assert client.payloads, "critique must have dispatched"
        assert len(client.payloads[0]["brief"]) <= 8000
