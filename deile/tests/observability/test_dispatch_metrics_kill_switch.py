"""AC7 + AC7b — kill-switches — issue #455.

AC7: ``DEILE_OBSERVABILITY_DISABLED=true`` → zero data points.
AC7b: ``DEILE_OTLP_METRICS_DISABLED=true`` (com endpoint set) → zero data
points; traces/logs (#443/#454) NÃO afetados.
"""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm
from deile.observability import reset_dispatch_metrics, reset_observability_config

# Os testes deste módulo instanciam um MeterProvider real do SDK OTel. O extra
# ``[otel]`` é opcional (DEILE roda em no-op sem ele), então sem o SDK o módulo
# inteiro é PULADO — mesmo contrato do conftest e dos demais testes de
# observability — em vez de hard-fail com ModuleNotFoundError.
pytest.importorskip("opentelemetry.sdk.metrics")

pytestmark = pytest.mark.unit


def _zero_points(reader):
    data = reader.get_metrics_data()
    if data is None:
        return True
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.data.data_points:
                    return False
    return True


def test_global_disabled_zero_points(monkeypatch):
    """AC7: kill-switch global → provider None, zero pontos."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(dm, "_meter_provider", provider)
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test:4317")
    monkeypatch.setenv("DEILE_OBSERVABILITY_DISABLED", "true")
    reset_observability_config()
    reset_dispatch_metrics()

    dm.record_dispatch_total(role="worker", outcome="completed")
    assert dm._get_dispatch_meter_provider() is None
    assert _zero_points(reader)
    provider.shutdown()


def test_metrics_disabled_isolated(monkeypatch):
    """AC7b: kill-switch isolado → métricas off, traces/logs intactos."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(dm, "_meter_provider", provider)
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test:4317")
    monkeypatch.setenv("DEILE_OTLP_METRICS_DISABLED", "true")
    reset_observability_config()
    reset_dispatch_metrics()

    dm.record_dispatch_total(role="worker", outcome="completed")
    assert dm._get_dispatch_meter_provider() is None
    assert _zero_points(reader)

    # Traces/logs NÃO afetados: a config dos outros sinais segue habilitada.
    from deile.observability.config import get_observability_config

    config = get_observability_config()
    assert config.metrics_disabled is True
    assert config.logs_disabled is False
    assert config.disabled is False
    assert config.is_enabled is True  # endpoint set → traces/logs seguem ligados
    provider.shutdown()
