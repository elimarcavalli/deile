"""Wiring test: resolve_stage_cost_cap_usd level-4 (global settings.json) — issue #666.

Verifica que ``pipeline.cost_cap_usd`` em settings.json é lido como fallback
global pelo resolver quando não há cap por-estágio nem env var global.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from deile.config.settings import get_settings, reset_settings
from deile.orchestration.pipeline.dispatch_resolver import (
    PIPELINE_STAGES,
    resolve_stage_cost_cap_usd,
)


def _clear_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(
            f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}", raising=False
        )
    monkeypatch.delenv("DEILE_PIPELINE_COST_CAP_USD", raising=False)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _clear_env(monkeypatch)
    reset_settings()
    _deile_logger = logging.getLogger("deile")
    _saved = _deile_logger.propagate
    _deile_logger.propagate = True
    yield
    reset_settings()
    _deile_logger.propagate = _saved


class TestLevel4GlobalSettings:
    def test_global_settings_returned_when_no_other_cap(self):
        """Level 4: pipeline_cost_cap_usd in settings is the fallback for all stages."""
        get_settings().pipeline_cost_cap_usd = Decimal("3.50")
        for stage in PIPELINE_STAGES:
            assert resolve_stage_cost_cap_usd(stage) == Decimal(
                "3.50"
            ), f"stage {stage!r} did not pick up global settings cap"

    def test_global_settings_beaten_by_env_global(self, monkeypatch):
        """Level 3 (env global) beats level 4 (global settings)."""
        get_settings().pipeline_cost_cap_usd = Decimal("3.50")
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD", "1.00")
        assert resolve_stage_cost_cap_usd("implement") == Decimal("1.00")

    def test_global_settings_beaten_by_per_stage_settings(self):
        """Level 2 (per-stage settings) beats level 4 (global settings)."""
        get_settings().pipeline_cost_cap_usd = Decimal("3.50")
        get_settings().pipeline_cost_cap_usd_implement = Decimal("8.00")
        assert resolve_stage_cost_cap_usd("implement") == Decimal("8.00")
        # Other stages still fall back to global
        assert resolve_stage_cost_cap_usd("classify") == Decimal("3.50")

    def test_global_settings_beaten_by_env_per_stage(self, monkeypatch):
        """Level 1 (env per-stage) beats level 4 (global settings)."""
        get_settings().pipeline_cost_cap_usd = Decimal("3.50")
        monkeypatch.setenv("DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT", "2.00")
        assert resolve_stage_cost_cap_usd("implement") == Decimal("2.00")
        assert resolve_stage_cost_cap_usd("classify") == Decimal("3.50")

    def test_none_when_global_settings_not_set(self):
        """Level 5 (no cap) returned when global settings not set."""
        assert resolve_stage_cost_cap_usd("implement") is None

    def test_invalid_global_settings_falls_through_to_none(self, caplog):
        """Invalid global settings value is ignored gracefully → None."""
        settings = get_settings()
        # Bypass the validator by directly setting an invalid value
        object.__setattr__(settings, "pipeline_cost_cap_usd", Decimal("-1"))
        with caplog.at_level(
            logging.WARNING, logger="deile.orchestration.pipeline.dispatch_resolver"
        ):
            result = resolve_stage_cost_cap_usd("implement")
        assert result is None
