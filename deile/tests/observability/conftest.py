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
    from deile.observability import (
        reset_dispatch_export,
        reset_dispatch_log_export,
        reset_dispatch_metrics,
        reset_metrics,
        reset_observability_config,
        reset_tracer,
    )

    for env in (
        "DEILE_OTLP_ENDPOINT",
        "DEILE_OTLP_HEADERS",
        "DEILE_OTLP_INSECURE",
        "DEILE_OTLP_SERVICE_NAME",
        "DEILE_OTLP_SAMPLE_RATIO",
        "DEILE_OBSERVABILITY_DISABLED",
        "DEILE_OTLP_LOGS_DISABLED",
        "DEILE_OTLP_METRICS_DISABLED",
        "DEILE_OTLP_SEMCONV_ENABLED",
        "OTEL_METRIC_EXPORT_INTERVAL",
        "DEILE_ROLE",
        "HOSTNAME",
    ):
        monkeypatch.delenv(env, raising=False)
    reset_tracer()
    reset_metrics()
    reset_observability_config()
    reset_dispatch_export()
    reset_dispatch_log_export()
    reset_dispatch_metrics()
    try:
        yield
    finally:
        reset_tracer()
        reset_metrics()
        reset_observability_config()
        reset_dispatch_export()
        reset_dispatch_log_export()
        reset_dispatch_metrics()


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
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("deile.observability.tracer._provider", provider)

    # Registra também como TracerProvider GLOBAL do OpenTelemetry. Os testes que
    # usam a API nativa (``trace.get_tracer`` / ``propagate.inject``) leem o
    # provider global — sem isto, ``get_tracer_provider()`` devolve o proxy NoOp
    # (``_TRACER_PROVIDER is None``) e nenhum span/traceparent é gravado. O
    # ``set_tracer_provider`` oficial só registra uma vez por processo, então em
    # teste setamos o atributo direto via monkeypatch (revertido no teardown,
    # não vaza para outros testes).
    import opentelemetry.trace as _ot_trace

    monkeypatch.setattr(_ot_trace, "_TRACER_PROVIDER", provider, raising=False)

    # Ligar OTLP via env para get_tracer() escolher OtlpTracer.
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test-collector:4317")

    from deile.observability import reset_observability_config, reset_tracer

    reset_observability_config()
    reset_tracer()

    yield exporter

    provider.shutdown()


@pytest.fixture
def in_memory_log_exporter(monkeypatch):
    """Injeta um LoggerProvider in-memory para inspeção dos log records.

    Substitui ``deile.observability.dispatch_log_export._log_provider`` por um
    ``LoggerProvider`` com ``InMemoryLogExporter`` via ``SimpleLogRecordProcessor``
    (sync, sem batch). O teste usa ``exporter.get_finished_logs()`` para inspecionar.

    Requer SDK com suporte a Logs instalado — pula o teste se não houver.
    """
    if not otel_sdk_available():
        pytest.skip("opentelemetry SDK não instalado (pip install -e .[otel])")

    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogExporter,
            SimpleLogRecordProcessor,
        )
    except ImportError:
        pytest.skip("opentelemetry SDK logs não disponível")

    log_exporter = InMemoryLogExporter()
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    monkeypatch.setattr(
        "deile.observability.dispatch_log_export._log_provider", log_provider
    )

    # Ligar OTLP via env para get_log_provider() não retornar None.
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test-collector:4317")

    from deile.observability import (
        reset_dispatch_log_export,
        reset_observability_config,
    )

    reset_observability_config()
    reset_dispatch_log_export()

    yield log_exporter

    log_provider.shutdown()


@pytest.fixture
def in_memory_dispatch_metrics_reader(monkeypatch):
    """Injeta um MeterProvider in-memory no módulo ``dispatch_metrics`` (#455).

    Substitui ``deile.observability.dispatch_metrics._meter_provider`` por um
    ``MeterProvider`` com ``InMemoryMetricReader``. O teste chama
    ``reader.get_metrics_data()`` para conferir nomes/labels/valores das
    métricas de dispatch — exercitando a fiação REAL de ``emit_*``.
    """
    if not otel_sdk_available():
        pytest.skip("opentelemetry SDK não instalado (pip install -e .[otel])")

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        "deile.observability.dispatch_metrics._meter_provider", provider
    )

    # Ligar OTLP via env para get_dispatch_meter_provider() não retornar None.
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://test-collector:4317")

    from deile.observability import reset_dispatch_metrics, reset_observability_config

    reset_observability_config()
    reset_dispatch_metrics()

    yield reader

    provider.shutdown()


def dispatch_metric_points(reader, metric_name):
    """Helper: retorna lista de (value/sum, attrs) p/ um metric do reader."""
    data = reader.get_metrics_data()
    points = []
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != metric_name:
                    continue
                for dp in metric.data.data_points:
                    value = getattr(dp, "value", None)
                    if value is None:  # histogram → use sum
                        value = getattr(dp, "sum", None)
                    points.append((value, dict(dp.attributes)))
    return points


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
