"""Tests for MetricsCollector."""

from __future__ import annotations

import json

from deile_bot.foundation.metrics import MetricsCollector, timer


class TestCounters:
    def test_inc_default(self):
        m = MetricsCollector()
        m.inc("c1")
        m.inc("c1")
        snap = m.snapshot()
        assert snap["counters"]["c1"][0]["value"] == 2

    def test_inc_with_labels(self):
        m = MetricsCollector()
        m.inc("c1", {"provider": "discord"})
        m.inc("c1", {"provider": "telegram"})
        snap = m.snapshot()
        labels_set = {tuple(sorted(s["labels"].items())) for s in snap["counters"]["c1"]}
        assert (("provider", "discord"),) in labels_set
        assert (("provider", "telegram"),) in labels_set


class TestGauges:
    def test_gauge_set(self):
        m = MetricsCollector()
        m.gauge("g1", value=4.5)
        m.gauge("g1", value=7.0)
        snap = m.snapshot()
        assert snap["gauges"]["g1"][0]["value"] == 7.0


class TestHistograms:
    def test_observe_count_sum(self):
        m = MetricsCollector()
        m.observe("h1", {"x": "y"}, 0.5)
        m.observe("h1", {"x": "y"}, 1.0)
        snap = m.snapshot()
        h = snap["histograms"]["h1"][0]
        assert h["count"] == 2
        assert abs(h["sum"] - 1.5) < 1e-9


class TestSerialization:
    def test_snapshot_is_json(self):
        m = MetricsCollector()
        m.inc("a")
        m.observe("b", value=0.1)
        m.gauge("c", value=5)
        snap = m.snapshot()
        json.dumps(snap)


class TestTimer:
    def test_timer_records(self):
        m = MetricsCollector()
        with timer(m, "h1"):
            pass
        snap = m.snapshot()
        assert snap["histograms"]["h1"][0]["count"] == 1
