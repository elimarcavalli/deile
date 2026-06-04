"""Tests for WorkerImplementer cost-cap gating (issue #392).

Covers:
- POST proceeds when no cost cap is configured (None)
- POST is blocked and motivo_bloqueio set when estimated cost > cap
- POST proceeds when estimated cost <= cap
- Guard exceptions (non-StageCostCapExceeded) never crash dispatch
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.orchestration.pipeline.implementer import WorkerImplementer


class _FakeClient:
    def __init__(self, response=None):
        self._response = response or {"task_id": "x", "status": "running"}
        self.calls = 0
        self.last_payload = None

    async def dispatch(self, payload, *, wait):
        self.calls += 1
        self.last_payload = payload
        return self._response


def _make_monitor():
    monitor = MagicMock()
    monitor.config = SimpleNamespace(
        repo="owner/repo", main_branch="main", base_repo_path=Path("/tmp/fake"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    return monitor


def _issue(number=1, title="t", body="brief body"):
    return SimpleNamespace(number=number, title=title, body=body)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
    from deile.config.settings import reset_settings
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_COST_CAP_USD", raising=False)
    reset_settings()
    yield
    reset_settings()


class TestImplementerCostCapGating:
    async def test_no_cap_dispatch_proceeds(self, monkeypatch):
        """When no cost cap is configured, dispatch proceeds normally."""
        client = _FakeClient()
        impl = WorkerImplementer(client=client)

        with patch(
            "deile.storage.usage_repository.get_usage_repository",
            return_value=MagicMock(),
        ):
            out = await impl.implement(_make_monitor(), _issue())

        assert out.ok is True
        assert client.calls == 1

    async def test_cap_exceeded_blocks_dispatch(self, monkeypatch):
        """When estimated cost > cap, dispatch is blocked and POST is NOT called."""
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "1.00")

        client = _FakeClient()
        impl = WorkerImplementer(client=client)

        mock_estimator = MagicMock()
        mock_estimator.estimate_run_cost.return_value = Decimal("5.00")

        with patch(
            "deile.storage.usage_repository.get_usage_repository",
            return_value=MagicMock(),
        ), patch(
            "deile.orchestration.pipeline.cost_estimator.StageCostEstimator",
            return_value=mock_estimator,
        ):
            out = await impl.implement(_make_monitor(), _issue())

        assert out.ok is False
        assert client.calls == 0
        assert out.motivo_bloqueio is not None
        assert "cost-cap-exceeded" in out.motivo_bloqueio

    async def test_cap_not_exceeded_dispatch_proceeds(self, monkeypatch):
        """When estimated cost <= cap, dispatch proceeds normally."""
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "10.00")

        client = _FakeClient()
        impl = WorkerImplementer(client=client)

        mock_estimator = MagicMock()
        mock_estimator.estimate_run_cost.return_value = Decimal("2.00")

        with patch(
            "deile.storage.usage_repository.get_usage_repository",
            return_value=MagicMock(),
        ), patch(
            "deile.orchestration.pipeline.cost_estimator.StageCostEstimator",
            return_value=mock_estimator,
        ):
            out = await impl.implement(_make_monitor(), _issue())

        assert out.ok is True
        assert client.calls == 1

    async def test_guard_error_non_fatal_dispatch_proceeds(self, monkeypatch):
        """A non-StageCostCapExceeded guard exception never crashes dispatch."""
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "1.00")

        client = _FakeClient()
        impl = WorkerImplementer(client=client)

        mock_estimator = MagicMock()
        mock_estimator.estimate_run_cost.side_effect = RuntimeError("db error")

        with patch(
            "deile.storage.usage_repository.get_usage_repository",
            return_value=MagicMock(),
        ), patch(
            "deile.orchestration.pipeline.cost_estimator.StageCostEstimator",
            return_value=mock_estimator,
        ):
            out = await impl.implement(_make_monitor(), _issue())

        assert out.ok is True
        assert client.calls == 1

    async def test_blocked_outcome_has_cost_details(self, monkeypatch):
        """Blocked WorkOutcome includes estimated and cap USD values."""
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "3.00")

        client = _FakeClient()
        impl = WorkerImplementer(client=client)

        mock_estimator = MagicMock()
        mock_estimator.estimate_run_cost.return_value = Decimal("7.50")

        with patch(
            "deile.storage.usage_repository.get_usage_repository",
            return_value=MagicMock(),
        ), patch(
            "deile.orchestration.pipeline.cost_estimator.StageCostEstimator",
            return_value=mock_estimator,
        ):
            out = await impl.implement(_make_monitor(), _issue())

        assert out.ok is False
        assert "7.50" in out.motivo_bloqueio
        assert "3.00" in out.motivo_bloqueio
