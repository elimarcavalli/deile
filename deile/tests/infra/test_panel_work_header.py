"""Testes do header WORK/LAST_COMPLETED em ``PodWatchView`` (issue #396).

Cobertura:

1. ``WorkerProvider._parse`` extrai ``model`` do ``dispatch.received``.
2. ``WorkerProvider._parse`` cria ``LastCompletedTask`` ao parear
   ``dispatch.received`` ↔ ``dispatch.completed``.
3. ``WorkerProvider._parse`` resolve ``cost_usd`` via ``CostsProvider``
   quando disponível; ``None`` quando DB ausente.
4. ``WorkerProvider._parse`` mantém ``last_completed = None`` quando
   o buffer só tem ``dispatch.received`` (sem completed).
5. ``PodWatchView._header_body`` renderiza linha ``WORK`` com modelo.
6. ``PodWatchView._header_body`` renderiza ``WORK: — (idle)`` quando idle.
7. ``PodWatchView._header_body`` renderiza ``LAST_COMPLETED`` com outcome
   colorido (green/red/dim).
8. ``PodWatchView._header_body`` não levanta erro quando ambos são ``None``.
9. Timestamp de ``LAST_COMPLETED`` usa formato ``Z`` (UTC explícito).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_s: int = 0) -> str:
    """Timestamp string no formato ``kubectl logs --timestamps``."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=offset_s)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000000000+00:00")


def _build_provider(costs=None) -> pd.WorkerProvider:
    prov = pd.WorkerProvider(ttl_s=0.0, costs=costs)
    prov._kubectl = "kubectl"
    return prov


def _render(view: panel.PodWatchView) -> str:
    """Captura o output Rich do _header_body como string plana."""
    renderable = view._header_body()
    console = Console(record=True, width=200, force_terminal=False,
                      color_system=None)
    console.print(renderable)
    return console.export_text()


def _worker_view(current_task=None, last_completed=None, busy: bool = False,
                 role: str = "worker") -> panel.PodWatchView:
    view = panel.PodWatchView(data=MagicMock())
    view.pod_role = role
    view.pod_name = "deile-worker-abc"
    pod = MagicMock()
    pod.name = view.pod_name
    pod.role = role
    pod.status = "Running"
    pod.age_s = 120.0
    pod.restarts = 0
    pod.ready = True
    pod.node = "node-1"
    view.data.pods.get.return_value = [pod]
    wstate = pd.WorkerState(
        pod_name=view.pod_name,
        busy=busy,
        current_task=current_task,
        last_completed=last_completed,
    )
    view.data.workers.get.return_value = {view.pod_name: wstate}
    view.data.claude_workers = None
    return view


# ---------------------------------------------------------------------------
# WorkerProvider._parse — model extraction
# ---------------------------------------------------------------------------

class TestWorkerProviderExtractsModel:
    def test_extracts_model_from_dispatch_received(self):
        prov = _build_provider()
        text = (
            f"{_ts(5)} dispatch.received task=abc123def456 "
            f"channel=pipeline-issue-396 stage=implement issue=396 "
            f"model=anthropic:claude-sonnet-4-6 branch=auto/issue-396"
        )
        state = prov._parse("w-1", text)
        assert state.current_task is not None
        assert state.current_task.model == "anthropic:claude-sonnet-4-6"

    def test_model_none_when_absent_in_started(self):
        prov = _build_provider()
        text = (
            f"{_ts(5)} dispatch.received task=abc123def456 "
            f"channel=pipeline-issue-396 stage=implement issue=396"
        )
        state = prov._parse("w-1", text)
        assert state.current_task is not None
        assert state.current_task.model is None

    def test_model_preserved_after_second_dispatch_received(self):
        """Dois dispatch.received sobrepostos — o mais recente prevalece."""
        prov = _build_provider()
        text = "\n".join([
            f"{_ts(10)} dispatch.received task=old1old1old1 "
            f"channel=pipeline-issue-100 model=anthropic:claude-opus-4-8",
            f"{_ts(8)} dispatch.completed task=old1old1old1 ok=True",
            f"{_ts(5)} dispatch.received task=new1new1new1 "
            f"channel=pipeline-issue-396 model=anthropic:claude-sonnet-4-6",
        ])
        state = prov._parse("w-1", text)
        assert state.current_task is not None
        assert state.current_task.model == "anthropic:claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# WorkerProvider._parse — LastCompletedTask creation
# ---------------------------------------------------------------------------

class TestWorkerProviderLastCompleted:
    def test_pairs_started_with_completed(self):
        prov = _build_provider()
        text = "\n".join([
            f"{_ts(10)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-issue-386 stage=pr_review issue=386 "
            f"model=anthropic:claude-sonnet-4-6",
            f"{_ts(3)} dispatch.completed task=aabbccdd1122 ok=True",
        ])
        state = prov._parse("w-1", text)
        assert state.current_task is None  # task completed — idle now
        assert state.last_completed is not None
        lc = state.last_completed
        assert lc.task_id == "aabbccdd1122"
        assert lc.channel_id == "pipeline-issue-386"
        assert lc.stage == "pr_review"
        assert lc.issue_number == 386
        assert lc.outcome == "DONE"
        assert lc.duration_s > 0

    def test_outcome_fail_when_ok_false(self):
        prov = _build_provider()
        text = "\n".join([
            f"{_ts(10)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-issue-1 issue=1",
            f"{_ts(3)} dispatch.completed task=aabbccdd1122 ok=False",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        assert state.last_completed.outcome == "FAIL"

    def test_outcome_from_explicit_outcome_field(self):
        """When dispatch.completed carries ``outcome=APPROVE``, that wins
        over the ok= field normalization."""
        prov = _build_provider()
        text = "\n".join([
            f"{_ts(10)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-pr-291 issue=291",
            f"{_ts(3)} dispatch.completed task=aabbccdd1122 ok=True outcome=APPROVE",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        assert state.last_completed.outcome == "APPROVE"

    def test_last_completed_none_when_only_started(self):
        """In-flight task — no completed line — last_completed stays None."""
        prov = _build_provider()
        text = (
            f"{_ts(5)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-issue-396 issue=396"
        )
        state = prov._parse("w-1", text)
        assert state.current_task is not None
        assert state.last_completed is None

    def test_last_completed_is_most_recent(self):
        """With two completed tasks, last_completed reflects the later one."""
        prov = _build_provider()
        text = "\n".join([
            f"{_ts(20)} dispatch.received task=aaaa11110000 "
            f"channel=pipeline-issue-100 issue=100",
            f"{_ts(15)} dispatch.completed task=aaaa11110000 ok=True",
            f"{_ts(10)} dispatch.received task=bbbb22220000 "
            f"channel=pipeline-issue-200 issue=200",
            f"{_ts(5)} dispatch.completed task=bbbb22220000 ok=False",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        assert state.last_completed.task_id == "bbbb22220000"
        assert state.last_completed.outcome == "FAIL"

    def test_duration_calculated_correctly(self):
        prov = _build_provider()
        text = "\n".join([
            f"{_ts(60)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-issue-1 issue=1",
            f"{_ts(13)} dispatch.completed task=aabbccdd1122 ok=True",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        # Duration should be approximately 47s (60 - 13), allow some drift
        assert 44 <= state.last_completed.duration_s <= 50

    def test_completed_without_matching_started_does_not_crash(self):
        """A dangling dispatch.completed (no matching started) is silently
        ignored — no exception, no last_completed."""
        prov = _build_provider()
        text = f"{_ts(3)} dispatch.completed task=orphan12345 ok=True"
        state = prov._parse("w-1", text)
        assert state.last_completed is None


# ---------------------------------------------------------------------------
# WorkerProvider._parse — cost_usd resolution
# ---------------------------------------------------------------------------

class TestWorkerProviderCostResolution:
    def _make_db(self, tmp_path: Path, task_id: str, cost: float) -> Path:
        db = tmp_path / "usage.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE usage_records "
            "(session_id TEXT, provider_id TEXT, cost_usd REAL, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO usage_records VALUES (?, ?, ?, ?)",
            (f"worker_{task_id}", "anthropic", cost, 1000.0),
        )
        conn.commit()
        conn.close()
        return db

    def test_cost_resolved_from_db(self, tmp_path):
        tid = "aabbccdd1122"
        db = self._make_db(tmp_path, tid, 0.32)
        costs = pd.CostsProvider(db_path=db)
        prov = _build_provider(costs=costs)
        text = "\n".join([
            f"{_ts(10)} dispatch.received task={tid} channel=pipeline-issue-386 issue=386",
            f"{_ts(3)} dispatch.completed task={tid} ok=True",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        assert state.last_completed.cost_usd == pytest.approx(0.32, rel=1e-4)

    def test_cost_none_when_db_missing(self):
        costs = pd.CostsProvider(db_path=Path("/nonexistent/usage.db"))
        prov = _build_provider(costs=costs)
        text = "\n".join([
            f"{_ts(10)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-issue-1 issue=1",
            f"{_ts(3)} dispatch.completed task=aabbccdd1122 ok=True",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        assert state.last_completed.cost_usd is None

    def test_cost_none_when_no_provider(self):
        prov = _build_provider(costs=None)
        text = "\n".join([
            f"{_ts(10)} dispatch.received task=aabbccdd1122 "
            f"channel=pipeline-issue-1 issue=1",
            f"{_ts(3)} dispatch.completed task=aabbccdd1122 ok=True",
        ])
        state = prov._parse("w-1", text)
        assert state.last_completed is not None
        assert state.last_completed.cost_usd is None


# ---------------------------------------------------------------------------
# PodWatchView._header_body — WORK line rendering
# ---------------------------------------------------------------------------

class TestPodWatchViewWorkLine:
    def test_work_line_shows_target_label_and_model(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-396",
            started_ts=datetime.now(timezone.utc),
            stage="implement", issue_number=396,
            branch="auto/issue-396",
            model="anthropic:claude-sonnet-4-6",
        )
        view = _worker_view(current_task=ct, busy=True)
        out = _render(view)
        assert "WORK:" in out
        assert "#396" in out
        assert "anthropic:claude-sonnet-4-6" in out
        assert "auto/issue-396" in out
        # Old "current task:" line should be gone
        assert "current task:" not in out

    def test_work_line_idle_when_no_current_task(self):
        view = _worker_view(current_task=None, busy=False)
        out = _render(view)
        assert "WORK:" in out
        assert "idle" in out.lower()

    def test_work_line_without_model_omits_model_segment(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-1",
            started_ts=datetime.now(timezone.utc),
            issue_number=1, model=None,
        )
        view = _worker_view(current_task=ct, busy=True)
        out = _render(view)
        assert "WORK:" in out
        assert "model" not in out

    def test_no_error_when_both_current_and_last_completed_none(self):
        view = _worker_view(current_task=None, last_completed=None)
        out = _render(view)  # must not raise
        assert "WORK:" in out
        assert "LAST_COMPLETED" not in out


# ---------------------------------------------------------------------------
# PodWatchView._header_body — LAST_COMPLETED line rendering
# ---------------------------------------------------------------------------

class TestPodWatchViewLastCompletedLine:
    def _make_lc(self, outcome: str, cost_usd=None, issue_number=386) -> pd.LastCompletedTask:
        return pd.LastCompletedTask(
            task_id="deadbeef1234",
            channel_id=f"pipeline-issue-{issue_number}",
            finished_ts=datetime(2026, 5, 29, 5, 23, 59, tzinfo=timezone.utc),
            outcome=outcome,
            duration_s=47.0,
            cost_usd=cost_usd,
            stage="pr_review",
            issue_number=issue_number,
        )

    def test_last_completed_shows_approve_green(self):
        lc = self._make_lc("APPROVE", cost_usd=0.32)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        assert "LAST_COMPLETED:" in out
        assert "APPROVE" in out
        assert "$0.32" in out
        # Timestamp with Z suffix
        assert "2026-05-29T05:23:59Z" in out

    def test_last_completed_shows_done_green(self):
        lc = self._make_lc("DONE", cost_usd=0.10)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        assert "DONE" in out
        assert "$0.10" in out

    def test_last_completed_shows_fail_red(self):
        lc = self._make_lc("FAIL", cost_usd=0.05)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        assert "LAST_COMPLETED:" in out
        assert "FAIL" in out

    def test_last_completed_shows_reject(self):
        lc = self._make_lc("REJECT", cost_usd=None)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        assert "REJECT" in out

    def test_last_completed_cost_unavailable_shows_placeholder(self):
        lc = self._make_lc("DONE", cost_usd=None)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        assert "ledger unavailable" in out or "$?" in out

    def test_last_completed_omitted_when_none(self):
        view = _worker_view(current_task=None, last_completed=None)
        out = _render(view)
        assert "LAST_COMPLETED" not in out

    def test_timestamp_uses_z_suffix(self):
        lc = self._make_lc("DONE", cost_usd=0.01)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        # Must end in Z, not +00:00
        assert "2026-05-29T05:23:59Z" in out
        assert "+00:00" not in out

    def test_last_completed_issue_label_uses_target_label(self):
        lc = self._make_lc("APPROVE", cost_usd=0.20, issue_number=386)
        view = _worker_view(last_completed=lc)
        out = _render(view)
        assert "#386" in out

    def test_non_worker_pod_no_last_completed(self):
        """Pods that are not worker/claude-worker never show LAST_COMPLETED."""
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "pipeline"
        view.pod_name = "deile-pipeline-xyz"
        pod = MagicMock()
        pod.name = view.pod_name
        pod.role = "pipeline"
        pod.status = "Running"
        pod.age_s = 60.0
        pod.restarts = 0
        pod.ready = True
        pod.node = "n1"
        view.data.pods.get.return_value = [pod]
        view.data.workers.get.return_value = {}
        view.data.claude_workers = None
        out = _render(view)
        assert "LAST_COMPLETED" not in out
        assert "WORK:" not in out
