"""Unit tests for ``CostTracker.list_budget_limits`` reload semantics."""
from __future__ import annotations

from decimal import Decimal

import pytest

from deile.infrastructure.monitoring.cost_tracker import CostTracker


@pytest.mark.unit
def test_list_budget_limits_returns_persisted(tmp_path):
    tracker = CostTracker(db_path=str(tmp_path / "costs.db"))
    tracker.set_budget_limit("api_calls", "monthly", 100)

    limits = tracker.list_budget_limits()

    assert "api_calls_monthly" in limits
    assert limits["api_calls_monthly"].limit_amount == Decimal("100")


@pytest.mark.unit
def test_list_budget_limits_reload_drops_stale_entries(tmp_path):
    tracker = CostTracker(db_path=str(tmp_path / "costs.db"))
    # Inject a stale in-memory entry not backed by the DB; an authoritative
    # reload must drop it instead of letting it linger.
    tracker.budget_limits["ghost_monthly"] = object()

    limits = tracker.list_budget_limits()

    assert "ghost_monthly" not in limits
