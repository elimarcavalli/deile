"""Unit tests for StageBudgetGuard + StageCostCapExceeded — issue #392.

Covers:
- check_stage_run passes when no cap configured (None)
- check_stage_run passes when estimate <= cap
- check_stage_run raises StageCostCapExceeded when estimate > cap
- check_stage_run passes when estimate is 0 (unknown pricing)
- resolve_stage_cost_cap_usd fallback chain (env var levels)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from deile.orchestration.pipeline.cost_estimator import reset_pricing_provider
from deile.storage.usage_repository import (
    StageBudgetGuard,
    StageCostCapExceeded,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Clear env vars + reset settings singleton before each test."""
    from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}",
                          raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_COST_CAP_USD", raising=False)
    reset_pricing_provider()
    from deile.config.settings import reset_settings
    reset_settings()
    yield
    reset_settings()
    reset_pricing_provider()


def _make_guard(estimated_cost: Decimal) -> StageBudgetGuard:
    estimator = MagicMock()
    estimator.estimate_run_cost.return_value = estimated_cost
    return StageBudgetGuard(estimator)


class TestStageBudgetGuardNoCap:
    def test_no_cap_passes_silently(self, monkeypatch):
        """When no cap is configured, check_stage_run returns without raising."""
        guard = _make_guard(Decimal("99.99"))
        # No env var set → no cap → passes
        guard.check_stage_run("implement", "anthropic:claude-opus-4-8")

    def test_zero_estimate_passes_even_with_cap(self, monkeypatch):
        """Estimate of 0 (unknown pricing) passes regardless of cap."""
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "1.00")
        guard = _make_guard(Decimal("0"))
        # Pricing unknown → estimate 0 → no enforcement
        guard.check_stage_run("implement", "anthropic:claude-opus-4-8")


class TestStageBudgetGuardWithCap:
    def test_below_cap_passes(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "5.00")
        guard = _make_guard(Decimal("4.99"))
        guard.check_stage_run("implement", "anthropic:claude-opus-4-8")

    def test_equal_to_cap_passes(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "5.00")
        guard = _make_guard(Decimal("5.00"))
        guard.check_stage_run("implement", "anthropic:claude-opus-4-8")

    def test_above_cap_raises(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "5.00")
        guard = _make_guard(Decimal("5.01"))

        with pytest.raises(StageCostCapExceeded) as exc_info:
            guard.check_stage_run("implement", "anthropic:claude-opus-4-8")

        exc = exc_info.value
        assert exc.stage == "implement"
        assert exc.estimated_usd == Decimal("5.01")
        assert exc.cap_usd == Decimal("5.00")

    def test_exception_message_contains_values(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_CLASSIFY", "2.00")
        guard = _make_guard(Decimal("3.50"))

        with pytest.raises(StageCostCapExceeded) as exc_info:
            guard.check_stage_run("classify", "anthropic:claude-haiku-4-5")

        assert "3.50" in str(exc_info.value)
        assert "2.00" in str(exc_info.value)
        assert "classify" in str(exc_info.value)


class TestResolveStageCostCapUsd:
    """Tests for the resolve_stage_cost_cap_usd fallback chain."""

    def test_no_env_returns_none(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        assert resolve_stage_cost_cap_usd("implement") is None

    def test_stage_env_var_parsed(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "7.50")
        assert resolve_stage_cost_cap_usd("implement") == Decimal("7.50")

    def test_global_env_var_fallback(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD", "3.00")
        assert resolve_stage_cost_cap_usd("refine") == Decimal("3.00")

    def test_stage_env_overrides_global(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD", "3.00")
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_REFINE", "10.00")
        assert resolve_stage_cost_cap_usd("refine") == Decimal("10.00")
        # implement should fall back to global
        assert resolve_stage_cost_cap_usd("implement") == Decimal("3.00")

    def test_invalid_stage_raises(self):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        with pytest.raises(ValueError, match="unknown stage"):
            resolve_stage_cost_cap_usd("bad_stage")

    def test_non_positive_env_raises(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "-1.00")
        with pytest.raises(ValueError, match="positive"):
            resolve_stage_cost_cap_usd("implement")

    def test_non_decimal_env_raises(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "not_a_number")
        with pytest.raises(ValueError):
            resolve_stage_cost_cap_usd("implement")

    def test_empty_string_treated_as_none(self, monkeypatch):
        from deile.orchestration.pipeline.dispatch_resolver import (
            resolve_stage_cost_cap_usd,
        )
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "")
        assert resolve_stage_cost_cap_usd("implement") is None
