"""Fixtures de teste para o subpacote ``deile/observability/``.

Cada teste roda com:
  - Singletons de tracer/metrics/config resetados (sem contaminação cruzada).
  - Env de OTLP desligada por default (testes que querem ``OtlpTracer`` ligam
    explicitamente via ``monkeypatch.setenv``).

Os testes que precisam do SDK (``in_memory_exporter`` fixture) ficam isolados
sob skip condicional via :func:`otel_sdk_available`.
"""

from __future__ import annotations

import pytest


def otel_sdk_available() -> bool:
    """Retorna True se o SDK OpenTelemetry está disponível."""
    try:
        import opentelemetry  # noqa: F401
        import opentelemetry.sdk.trace  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """Reseta singletons + limpa envs OTLP antes/depois de cada teste."""
    from deile.observability import (reset_dispatch_export, reset_metrics,
                                     reset_observability_config, reset_tracer)

    for env in (
        "DEILE_OTLP_ENDPOINT",
        "DEILE_OTLP_HEADERS",
        "DEILE_OTLP_INSECURE",
        "DEILE_OTLP_SERVICE_NAME",
        "DEILE_OTLP_SAMPLE_RATIO",
        "DEILE_OBSERVABILITY_DISABLED",
        "DEILE_ROLE",
        "HOSTNAME",
    ):
        monkeypatch.delenv(env, raising=False)
    reset_tracer()
    reset_metrics()
    reset_observability_config()
    reset_dispatch_export()
    try:
        yield
    finally:
        reset_tracer()
        reset_metrics()
        reset_observability_config()
        reset_dispatch_export()


@pytest.fixture
def in_memory_exporter(monkeypatch):
    """Injeta um TracerProvider em-memória no módulo ``tracer``.

    Substitui ``deile.observability.tracer._provider`` por um ``TracerProvider``
    com ``InMemorySpanExporter`` via ``SimpleSpanProcessor`` (sync, sem batch).
    O teste usa ``exporter.get_finished_spans()`` para inspecionar.

    Requer SDK instalado — pula o teste se não houver.
    """
    if not otel_sdk_available():
        pytest.skip("opentelemetry SDK não instalado (pip install -e .[otel])")

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import \
        InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("deile.observability.tracer._provider", provider)

    # Ligar OTLP via env para get_tracer() escolher OtlpTracer.
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test-collector:4317")

    from deile.observability import reset_observability_config, reset_tracer
    reset_observability_config()
    reset_tracer()

    yield exporter

    provider.shutdown()


@pytest.fixture
def in_memory_metrics_reader(monkeypatch):
    """Injeta um MeterProvider in-memory para inspeção das métricas.

    Usa ``InMemoryMetricReader`` — o teste chama ``reader.get_metrics_data()``
    para conferir nomes/labels/valores.
    """
    if not otel_sdk_available():
        pytest.skip("opentelemetry SDK não instalado (pip install -e .[otel])")

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr("deile.observability.metrics._provider", provider)

    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test-collector:4317")

    from deile.observability import reset_metrics, reset_observability_config
    reset_observability_config()
    reset_metrics()

    yield reader

    provider.shutdown()
