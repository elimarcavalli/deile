"""AC9 — singleton idempotente — issue #455 D7."""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm

pytestmark = pytest.mark.unit


def test_same_provider_twice(in_memory_dispatch_metrics_reader):
    p1 = dm._get_dispatch_meter_provider()
    p2 = dm._get_dispatch_meter_provider()
    assert p1 is p2
    assert dm._init_count == 1


def test_instruments_created_once(in_memory_dispatch_metrics_reader):
    dm._get_dispatch_meter_provider()
    first = dm._instruments
    dm._get_dispatch_meter_provider()
    # Mesmo dict de instruments (não recriado).
    assert dm._instruments is first
    assert dm._init_count == 1


def test_reset_clears_singleton(in_memory_dispatch_metrics_reader):
    dm._get_dispatch_meter_provider()
    assert dm._init_count == 1
    dm.reset_dispatch_metrics()
    assert dm._init_count == 0
    assert dm._instruments == {}
    assert dm._provider_tried is False
