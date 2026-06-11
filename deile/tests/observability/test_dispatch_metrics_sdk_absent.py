"""AC8 — graceful degradation quando SDK de métricas ausente — issue #455 D5.

Verifica que:
- ``record_*`` é no-op silencioso (não lança).
- Linha INFO ``otel_sdk_available=false`` na primeira chamada (lazy), só 1×.
- Provider retorna None.
"""

from __future__ import annotations

import logging

import pytest

from deile.observability import (reset_dispatch_metrics,
                                 reset_observability_config)

pytestmark = pytest.mark.unit


class TestSdkAbsent:
    def test_no_op_when_metrics_sdk_absent(self, monkeypatch):
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        reset_observability_config()
        reset_dispatch_metrics()

        import deile.observability.dispatch_metrics as dm
        monkeypatch.setattr(dm, "metrics_available", lambda: False)

        # Não levanta.
        dm.record_dispatch_total(role="worker", outcome="completed")

    def test_info_line_on_first_call(self, monkeypatch, caplog):
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        reset_observability_config()
        reset_dispatch_metrics()

        import deile.observability.dispatch_metrics as dm
        monkeypatch.setattr(dm, "metrics_available", lambda: False)

        with caplog.at_level(logging.INFO, logger=dm._logger.name):
            dm._get_dispatch_meter_provider()

        assert "otel_sdk_available=false" in caplog.text
        assert "sink=disabled" in caplog.text

    def test_info_line_only_once(self, monkeypatch, caplog):
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        reset_observability_config()
        reset_dispatch_metrics()

        import deile.observability.dispatch_metrics as dm
        monkeypatch.setattr(dm, "metrics_available", lambda: False)

        with caplog.at_level(logging.INFO, logger=dm._logger.name):
            dm._get_dispatch_meter_provider()
            dm._get_dispatch_meter_provider()
            dm.record_dispatch_total(role="worker", outcome="completed")
            dm.record_git_push_total(outcome="ok")

        count = caplog.text.count("otel_sdk_available=false")
        assert count == 1, f"expected exactly 1 warning, got {count}"

    def test_sdk_warned_false_at_import(self):
        import deile.observability.dispatch_metrics as dm
        assert dm._sdk_warned is False

    def test_provider_none_when_sdk_absent(self, monkeypatch):
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        reset_observability_config()
        reset_dispatch_metrics()

        import deile.observability.dispatch_metrics as dm
        monkeypatch.setattr(dm, "metrics_available", lambda: False)

        assert dm._get_dispatch_meter_provider() is None
