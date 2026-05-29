"""Unit tests for per-stage cost cap settings (issue #392).

Covers:
- Settings dataclass has cost cap fields
- _to_optional_positive_decimal validator
- JSON loading via override handlers (pipeline.cost_caps_usd.<stage>)
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from deile.config.settings import (
    Settings,
    _to_optional_positive_decimal,
    get_settings,
    reset_settings,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_settings()
    yield
    reset_settings()


class TestToOptionalPositiveDecimal:
    def test_none_returns_none(self):
        assert _to_optional_positive_decimal(None) is None

    def test_empty_string_returns_none(self):
        assert _to_optional_positive_decimal("") is None
        assert _to_optional_positive_decimal("   ") is None

    def test_valid_string_returns_decimal(self):
        assert _to_optional_positive_decimal("5.00") == Decimal("5.00")
        assert _to_optional_positive_decimal("1") == Decimal("1")
        assert _to_optional_positive_decimal("0.50") == Decimal("0.50")

    def test_valid_int_returns_decimal(self):
        assert _to_optional_positive_decimal(5) == Decimal("5")

    def test_valid_float_returns_decimal(self):
        d = _to_optional_positive_decimal(2.5)
        assert d > 0

    def test_decimal_passthrough(self):
        assert _to_optional_positive_decimal(Decimal("3.14")) == Decimal("3.14")

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="positive"):
            _to_optional_positive_decimal("0")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="positive"):
            _to_optional_positive_decimal("-1.00")

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            _to_optional_positive_decimal("abc")

    def test_non_string_non_numeric_raises(self):
        with pytest.raises(TypeError):
            _to_optional_positive_decimal([1, 2])


class TestSettingsCostCapFields:
    def test_defaults_are_none(self):
        s = Settings()
        assert s.pipeline_cost_cap_usd_classify is None
        assert s.pipeline_cost_cap_usd_refine is None
        assert s.pipeline_cost_cap_usd_implement is None
        assert s.pipeline_cost_cap_usd_pr_review is None
        assert s.pipeline_cost_cap_usd_follow_ups is None

    def test_fields_accept_decimal(self):
        s = Settings(
            pipeline_cost_cap_usd_implement=Decimal("5.00"),
            pipeline_cost_cap_usd_classify=Decimal("1.50"),
        )
        assert s.pipeline_cost_cap_usd_implement == Decimal("5.00")
        assert s.pipeline_cost_cap_usd_classify == Decimal("1.50")


class TestSettingsJsonLoadingCostCap:
    def test_json_loads_cost_caps(self):
        """Applying cost_caps_usd overrides via apply_overrides."""
        cfg = {
            "pipeline": {
                "cost_caps_usd": {
                    "implement": "5.00",
                    "classify": "1.00",
                    "pr_review": "10.00",
                }
            }
        }
        s = Settings()
        s.apply_overrides(cfg)

        assert s.pipeline_cost_cap_usd_implement == Decimal("5.00")
        assert s.pipeline_cost_cap_usd_classify == Decimal("1.00")
        assert s.pipeline_cost_cap_usd_pr_review == Decimal("10.00")
        # Unset stages remain None
        assert s.pipeline_cost_cap_usd_refine is None
        assert s.pipeline_cost_cap_usd_follow_ups is None

    def test_invalid_value_is_skipped_gracefully(self):
        """A bad value is skipped (logged as warning), default stays."""
        cfg = {
            "pipeline": {
                "cost_caps_usd": {
                    "implement": "not_a_number",
                    "classify": "5.00",
                }
            }
        }
        s = Settings()
        s.apply_overrides(cfg)

        # Invalid value skipped → default None
        assert s.pipeline_cost_cap_usd_implement is None
        # Valid value applied
        assert s.pipeline_cost_cap_usd_classify == Decimal("5.00")
