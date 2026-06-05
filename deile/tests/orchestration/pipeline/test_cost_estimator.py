"""Unit tests for cost_estimator.py — issue #392.

Covers:
- StageCostEstimator.estimate_run_cost with mocked history
- Fallback heuristics when history is absent
- PricingProvider zero-fallback when model unknown
- payload_size_tokens override of historical prompt-token average
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from deile.orchestration.pipeline.cost_estimator import (
    _FALLBACK_TOKENS, StageCostEstimator, reset_pricing_provider)
from deile.storage.usage_repository import UsageRecord


@pytest.fixture(autouse=True)
def _reset_pricing():
    reset_pricing_provider()
    yield
    reset_pricing_provider()


def _make_record(prompt: int, completion: int, model: str = "anthropic:claude-sonnet-4-6") -> UsageRecord:
    return UsageRecord(
        provider_id="anthropic",
        model_id=model,
        tier="high",
        session_id="pipeline-implement-123",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def _make_pricing_provider(input_price: str = "0.000003",
                           output_price: str = "0.000015") -> MagicMock:
    pp = MagicMock()
    pp.get_pricing.return_value = (Decimal(input_price), Decimal(output_price))
    return pp


class TestStageCostEstimatorFallback:
    """When no history exists, estimator uses fallback token counts."""

    def test_no_history_uses_fallback_implement(self):
        repo = MagicMock()
        repo.records_for_stage_model.return_value = []
        pp = _make_pricing_provider("0.000003", "0.000015")
        estimator = StageCostEstimator(repo, pp)

        cost = estimator.estimate_run_cost("implement", "anthropic:claude-opus-4-8")

        fallback_in, fallback_out = _FALLBACK_TOKENS["implement"]
        expected = (
            Decimal(fallback_in) * Decimal("0.000003")
            + Decimal(fallback_out) * Decimal("0.000015")
        )
        assert cost == expected

    def test_no_history_uses_fallback_classify(self):
        repo = MagicMock()
        repo.records_for_stage_model.return_value = []
        pp = _make_pricing_provider("0.000001", "0.000005")
        estimator = StageCostEstimator(repo, pp)

        cost = estimator.estimate_run_cost("classify", "anthropic:claude-haiku-4-5")

        fallback_in, fallback_out = _FALLBACK_TOKENS["classify"]
        expected = (
            Decimal(fallback_in) * Decimal("0.000001")
            + Decimal(fallback_out) * Decimal("0.000005")
        )
        assert cost == expected

    def test_single_record_falls_back_to_heuristic(self):
        """Fewer than 2 records → fallback (not a stable average)."""
        repo = MagicMock()
        repo.records_for_stage_model.return_value = [
            _make_record(prompt=10_000, completion=5_000)
        ]
        pp = _make_pricing_provider("0.000003", "0.000015")
        estimator = StageCostEstimator(repo, pp)

        cost = estimator.estimate_run_cost("implement", "anthropic:claude-sonnet-4-6")

        # Should use fallback (30000 in / 15000 out), not the single record.
        fallback_in, fallback_out = _FALLBACK_TOKENS["implement"]
        expected = (
            Decimal(fallback_in) * Decimal("0.000003")
            + Decimal(fallback_out) * Decimal("0.000015")
        )
        assert cost == expected


class TestStageCostEstimatorHistory:
    """When history has >= 2 records, use their average."""

    def test_averages_prompt_and_completion_tokens(self):
        repo = MagicMock()
        repo.records_for_stage_model.return_value = [
            _make_record(prompt=10_000, completion=4_000),
            _make_record(prompt=6_000, completion=2_000),
        ]
        pp = _make_pricing_provider("0.000003", "0.000015")
        estimator = StageCostEstimator(repo, pp)

        cost = estimator.estimate_run_cost("implement", "anthropic:claude-sonnet-4-6")

        avg_in = int((10_000 + 6_000) / 2)   # 8000
        avg_out = int((4_000 + 2_000) / 2)    # 3000
        expected = (
            Decimal(avg_in) * Decimal("0.000003")
            + Decimal(avg_out) * Decimal("0.000015")
        )
        assert cost == expected

    def test_payload_size_overrides_prompt_average(self):
        """When payload_size_tokens > 0, it replaces the historical average."""
        repo = MagicMock()
        repo.records_for_stage_model.return_value = [
            _make_record(prompt=10_000, completion=4_000),
            _make_record(prompt=10_000, completion=4_000),
        ]
        pp = _make_pricing_provider("0.000003", "0.000015")
        estimator = StageCostEstimator(repo, pp)

        payload_tokens = 50_000
        cost = estimator.estimate_run_cost(
            "implement", "anthropic:claude-sonnet-4-6",
            payload_size_tokens=payload_tokens,
        )

        # prompt = payload_tokens, completion = average
        expected = (
            Decimal(payload_tokens) * Decimal("0.000003")
            + Decimal(4_000) * Decimal("0.000015")
        )
        assert cost == expected


class TestStageCostEstimatorPricingFallback:
    """When model pricing is unknown, estimate returns 0."""

    def test_unknown_model_returns_zero(self):
        repo = MagicMock()
        repo.records_for_stage_model.return_value = []
        pp = _make_pricing_provider("0", "0")
        estimator = StageCostEstimator(repo, pp)

        cost = estimator.estimate_run_cost("implement", "unknown:model-xyz")

        assert cost == Decimal(0)

    def test_repo_error_falls_back_gracefully(self):
        """UsageRepository raising → falls back to heuristic."""
        repo = MagicMock()
        repo.records_for_stage_model.side_effect = RuntimeError("db error")
        pp = _make_pricing_provider("0.000003", "0.000015")
        estimator = StageCostEstimator(repo, pp)

        # Should not raise; falls back to heuristic.
        cost = estimator.estimate_run_cost("implement", "anthropic:claude-opus-4-8")

        fallback_in, fallback_out = _FALLBACK_TOKENS["implement"]
        expected = (
            Decimal(fallback_in) * Decimal("0.000003")
            + Decimal(fallback_out) * Decimal("0.000015")
        )
        assert cost == expected
