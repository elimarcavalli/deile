"""AC5 — ``OTEL_METRIC_EXPORT_INTERVAL`` respeitado por ``_make_reader`` — #455.

D4: ``_make_reader`` lê a var OTel-padrão e a passa como
``export_interval_millis`` ao ``PeriodicExportingMetricReader``.
"""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm
from deile.observability.config import ObservabilityConfig

pytestmark = pytest.mark.unit


def _otlp_metric_exporter_available() -> bool:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: F401
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics.export import (  # noqa: F401
            PeriodicExportingMetricReader,
        )

        return True
    except ImportError:
        return False


def test_interval_helper_reads_env(monkeypatch):
    """A leitura da var OTel é independente do exporter gRPC (sempre roda)."""
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "100")
    assert dm._export_interval_ms() == 100


def test_interval_helper_default_when_unset(monkeypatch):
    monkeypatch.delenv("OTEL_METRIC_EXPORT_INTERVAL", raising=False)
    assert dm._export_interval_ms() == 60000


def test_interval_helper_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "not-a-number")
    assert dm._export_interval_ms() == 60000


def test_export_interval_from_env(monkeypatch):
    if not _otlp_metric_exporter_available():
        pytest.skip("OTLPMetricExporter (gRPC) não instalado")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "100")
    config = ObservabilityConfig(endpoint="http://test:4317")
    reader = dm._make_reader(config)
    assert reader is not None
    # SDK armazena o intervalo em _export_interval_millis (atributo interno).
    assert reader._export_interval_millis == 100
    try:
        reader.shutdown()
    except Exception:  # noqa: BLE001
        pass


def test_export_interval_default_when_unset(monkeypatch):
    if not _otlp_metric_exporter_available():
        pytest.skip("OTLPMetricExporter (gRPC) não instalado")
    monkeypatch.delenv("OTEL_METRIC_EXPORT_INTERVAL", raising=False)
    config = ObservabilityConfig(endpoint="http://test:4317")
    reader = dm._make_reader(config)
    assert reader is not None
    assert reader._export_interval_millis == 60000
    try:
        reader.shutdown()
    except Exception:  # noqa: BLE001
        pass
