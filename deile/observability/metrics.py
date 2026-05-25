"""DeileMetrics — counters/histograms emitidos via OpenTelemetry Metrics API.

Quando OTLP está desligado ou SDK ausente, :func:`get_metrics` retorna
:class:`NoOpMetrics` (de :mod:`deile.observability.no_op`).

Métricas emitidas (cardinality controlada — evitar ``session_id`` como label):

================================  ==========  =====================================
Métrica                           Tipo        Labels
================================  ==========  =====================================
``deile.tokens.total``            counter     provider, model, direction
``deile.cost.usd.total``          counter     provider, model
``deile.tool.duration_ms``        histogram   tool_name, status
``deile.turn.duration_ms``        histogram   persona
``deile.errors.total``            counter     error_type, component
================================  ==========  =====================================

Labels base (``deile.instance.id`` + ``deile.instance.role``) entram via
resource attribute do ``MeterProvider`` — não inflam cardinality por métrica.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from deile.observability.config import (ObservabilityConfig,
                                        get_observability_config)
from deile.observability.no_op import NoOpMetrics
from deile.observability.tracer import otel_available

logger = logging.getLogger(__name__)

__all__ = [
    "DeileMetrics",
    "OtlpMetrics",
    "NoOpMetrics",
    "get_metrics",
    "reset_metrics",
]


DeileMetrics = Any  # type: ignore[assignment]


class OtlpMetrics:
    """Coletor de métricas backed pela OpenTelemetry Metrics SDK.

    Singleton thread-safe. Setup lazy igual ao tracer — só inicializa o
    MeterProvider na primeira leitura/escrita real.
    """

    def __init__(self, config: Optional[ObservabilityConfig] = None) -> None:
        self._config = config or get_observability_config()
        self._provider: Any = None
        self._meter: Any = None
        self._setup_lock = threading.Lock()
        self._shutdown = False

        # Instrumentos (lazy — só criados depois do meter pronto)
        self._counter_tokens: Any = None
        self._counter_cost: Any = None
        self._hist_tool_duration: Any = None
        self._hist_turn_duration: Any = None
        self._counter_errors: Any = None

    # ── lazy setup ────────────────────────────────────────────────────────

    def _ensure_meter(self) -> Any:
        """Inicializa o ``MeterProvider`` no primeiro acesso (thread-safe)."""
        if self._meter is not None:
            return self._meter
        with self._setup_lock:
            if self._meter is not None:
                return self._meter
            try:
                self._build_provider()
            except Exception as exc:  # noqa: BLE001 — fail open
                logger.warning(
                    "OtlpMetrics setup failed (%s); falling back to no-op", exc
                )
                self._meter = None
                return None
            return self._meter

    def _build_provider(self) -> None:
        """Configura o ``MeterProvider`` apontando para o collector."""
        from opentelemetry.sdk.metrics import \
            MeterProvider  # pylint: disable=import-outside-toplevel
        from opentelemetry.sdk.resources import (  # pylint: disable=import-outside-toplevel
            SERVICE_NAME, Resource)

        # Permite que testes injetem um provider via monkeypatch.
        injected = _module_injected_provider()
        if injected is not None:
            self._provider = injected
            self._meter = injected.get_meter("deile")
            self._create_instruments()
            return

        resource = Resource.create({SERVICE_NAME: self._config.service_name})
        reader = self._make_reader()
        readers = [reader] if reader is not None else []
        provider = MeterProvider(resource=resource, metric_readers=readers)

        self._provider = provider
        self._meter = provider.get_meter("deile")
        self._create_instruments()

    def _make_reader(self) -> Any:
        """Constrói ``PeriodicExportingMetricReader`` + ``OTLPMetricExporter``."""
        try:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import \
                OTLPMetricExporter  # pylint: disable=import-outside-toplevel
            from opentelemetry.sdk.metrics.export import \
                PeriodicExportingMetricReader  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc (metrics) não disponível "
                "(%s); métricas não serão exportadas.",
                exc,
            )
            return None
        try:
            exporter = OTLPMetricExporter(
                endpoint=self._config.endpoint,
                insecure=self._config.insecure,
                headers=self._config.headers or None,
            )
            return PeriodicExportingMetricReader(exporter)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OTLPMetricExporter init failed: %s", exc)
            return None

    def _create_instruments(self) -> None:
        """Cria os instrumentos (counters/histograms) a partir do meter."""
        m = self._meter
        if m is None:
            return
        self._counter_tokens = m.create_counter(
            name="deile.tokens.total",
            description="Tokens consumidos por provider/model/direção (in|out|cached).",
            unit="tokens",
        )
        self._counter_cost = m.create_counter(
            name="deile.cost.usd.total",
            description="Custo acumulado em USD por provider/model.",
            unit="usd",
        )
        self._hist_tool_duration = m.create_histogram(
            name="deile.tool.duration_ms",
            description="Duração de execuções de tool por nome/status.",
            unit="ms",
        )
        self._hist_turn_duration = m.create_histogram(
            name="deile.turn.duration_ms",
            description="Duração de turnos do agente por persona.",
            unit="ms",
        )
        self._counter_errors = m.create_counter(
            name="deile.errors.total",
            description="Erros capturados por tipo/componente.",
            unit="1",
        )

    # ── record helpers ────────────────────────────────────────────────────

    def record_tokens(
        self,
        provider: str,
        model: str,
        direction: str,
        count: int,
    ) -> None:
        if not count or count <= 0:
            return
        self._ensure_meter()
        if self._counter_tokens is None:
            return
        try:
            self._counter_tokens.add(
                int(count),
                attributes={
                    "provider": str(provider),
                    "model": str(model),
                    "direction": str(direction),
                },
            )
        except Exception as exc:  # noqa: BLE001 — métricas nunca quebram
            logger.debug("record_tokens failed: %s", exc)

    def record_cost(
        self,
        provider: str,
        model: str,
        usd: float,
    ) -> None:
        if not usd or usd <= 0:
            return
        self._ensure_meter()
        if self._counter_cost is None:
            return
        try:
            self._counter_cost.add(
                float(usd),
                attributes={
                    "provider": str(provider),
                    "model": str(model),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_cost failed: %s", exc)

    def record_tool_duration(
        self,
        tool_name: str,
        status: str,
        duration_ms: int,
    ) -> None:
        self._ensure_meter()
        if self._hist_tool_duration is None:
            return
        try:
            self._hist_tool_duration.record(
                int(duration_ms),
                attributes={
                    "tool_name": str(tool_name),
                    "status": str(status),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_tool_duration failed: %s", exc)

    def record_turn_duration(
        self,
        persona: str,
        duration_ms: int,
    ) -> None:
        self._ensure_meter()
        if self._hist_turn_duration is None:
            return
        try:
            self._hist_turn_duration.record(
                int(duration_ms),
                attributes={
                    "persona": str(persona or "unknown"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_turn_duration failed: %s", exc)

    def record_error(
        self,
        error_type: str,
        component: str,
    ) -> None:
        self._ensure_meter()
        if self._counter_errors is None:
            return
        try:
            self._counter_errors.add(
                1,
                attributes={
                    "error_type": str(error_type),
                    "component": str(component),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_error failed: %s", exc)

    def shutdown(self) -> None:
        """Flush + shutdown do MeterProvider. Idempotente."""
        if self._shutdown:
            return
        self._shutdown = True
        provider = self._provider
        if provider is None:
            return
        try:
            shutdown_fn = getattr(provider, "shutdown", None)
            if callable(shutdown_fn):
                shutdown_fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("MeterProvider shutdown failed: %s", exc)


def _module_injected_provider() -> Any:
    """Retorna ``_provider`` do módulo (monkeypatch por testes)."""
    return globals().get("_provider", None)


# Hook de monkeypatch (vide ``_module_injected_provider``).
_provider: Any = None


# ── singleton ────────────────────────────────────────────────────────────

_metrics_singleton: Optional[Any] = None
_singleton_lock = threading.Lock()


def get_metrics() -> Any:
    """Retorna o coletor de métricas singleton (OTLP real ou no-op)."""
    global _metrics_singleton
    with _singleton_lock:
        if _metrics_singleton is None:
            config = get_observability_config()
            if config.is_enabled and otel_available():
                _metrics_singleton = OtlpMetrics(config=config)
            else:
                _metrics_singleton = NoOpMetrics()
        return _metrics_singleton


def reset_metrics() -> None:
    """Reseta o singleton — apenas para testes."""
    global _metrics_singleton
    with _singleton_lock:
        if _metrics_singleton is not None:
            try:
                _metrics_singleton.shutdown()
            except Exception:  # noqa: BLE001
                pass
            _metrics_singleton = None
