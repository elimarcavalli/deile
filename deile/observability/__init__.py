"""Observabilidade OpenTelemetry — traces + metrics estruturados.

Padrão CNCF: spans (``deile.turn`` / ``deile.tool.<name>`` / ``deile.llm.call``)
e métricas (``deile.tokens.total``, ``deile.cost.usd.total``,
``deile.tool.duration_ms``, ``deile.turn.duration_ms``, ``deile.errors.total``).

API pública:

    from deile.observability import get_tracer, get_metrics

    with get_tracer().turn(session_id=..., turn_number=...) as span:
        ...

    get_metrics().record_tokens(provider="anthropic", model="...", direction="in", count=1000)

Fallback: quando ``opentelemetry`` não está instalado OU
``DEILE_OTLP_ENDPOINT`` não está set, ambos retornam backends no-op (chamadas
seguras, sem custo). Configurável via env vars (ver
:class:`~deile.observability.config.ObservabilityConfig`).

Princípio 11 (observabilidade): logger plain + EventBus para o resto;
spans/métricas só para o que o operador precisa correlacionar via Tempo/Grafana.

Princípio 8 (segurança): nenhum atributo carrega prompt/args/response/conteúdo.
Apenas tamanhos (int), tokens (int), custo (float) e identificadores opacos.
"""

from deile.observability.config import (ObservabilityConfig,
                                        get_observability_config,
                                        reset_observability_config)
from deile.observability.metrics import (DeileMetrics, NoOpMetrics,
                                         OtlpMetrics, get_metrics,
                                         reset_metrics)
from deile.observability.no_op import NoOpSpan, NoOpTracer
from deile.observability.tracer import (DeileTracer, OtlpTracer, get_tracer,
                                        otel_available, reset_tracer)

__all__ = [
    # tracer
    "DeileTracer",
    "OtlpTracer",
    "NoOpTracer",
    "NoOpSpan",
    "get_tracer",
    "reset_tracer",
    "otel_available",
    # metrics
    "DeileMetrics",
    "OtlpMetrics",
    "NoOpMetrics",
    "get_metrics",
    "reset_metrics",
    # config
    "ObservabilityConfig",
    "get_observability_config",
    "reset_observability_config",
]
