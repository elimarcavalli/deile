"""Adapter OTLP metrics dos eventos dispatch.*/git.*/forge.* — issue #455.

Fecha a trinca de sinais OTLP: traces (#443, ``dispatch_export.py``) cobrem
investigação caso-a-caso; logs (#454, ``dispatch_log_export.py``) cobrem pesquisa
histórica via Loki; **métricas** (este módulo) cobrem agregados ao longo do tempo
(taxa de falhas por ``reason``, p50/p95/p99 da duração, buckets de tool-burst)
para dashboards e alertas Prometheus.

Decisões de arquitetura (D1-D8 da issue #455):

- **D1**: ``MeterProvider`` PRÓPRIO (não estende ``OtlpMetrics`` de ``metrics.py``,
  que expõe apenas API domain-specific). Paralelo ao ``LoggerProvider`` de #454.
  Mesma config via ``get_observability_config()``; resource attrs idênticos a
  #443/#454: ``service.name``, ``deile.role``, ``deile.pod``,
  ``deile.dispatch.schema_version``. Respeita ``DEILE_OBSERVABILITY_DISABLED``
  (global) e o kill-switch isolado ``DEILE_OTLP_METRICS_DISABLED``.
- **D2**: 7 instruments declarados uma vez em ``_init_instruments(meter)``.
  Constante ``_ALLOWED_LABELS`` mapeia cada métrica ao set fechado de label keys;
  ``record_*`` valida kwargs e raise ``ValueError`` para key fora do set
  (cardinality bounded). ``_tool_burst_bucket`` mapeia ``count`` → bucket fechado.
- **D3**: Hook em ``dispatch_export.emit_*`` — cada emit relevante chama o
  ``record_*`` correspondente APÓS a operação de span, em ``try/except`` isolado.
- **D4**: ``_make_reader()`` lê ``OTEL_METRIC_EXPORT_INTERVAL`` (var OTel-padrão,
  exceção documentada ao Princípio 7) e passa ao ``PeriodicExportingMetricReader``.
- **D5**: SDK ausente / endpoint vazio / kill-switch / collector unreachable →
  NoOp ou drop counter throttled ≤1×/60s. Nunca quebra o turn.
- **D6**: Sem novas dependências. ``opentelemetry-sdk`` + exporter OTLP já em
  ``pyproject.toml``. Graceful no-op quando SDK ausente.
- **D7**: Singleton thread-safe (``threading.Lock`` + ``_provider_tried``).
  Idempotente: ``_init_count`` exatamente 1 após init bem-sucedida.
  ``reset_dispatch_metrics()`` / ``shutdown_dispatch_metrics()`` para testes.
- **D8**: Documentado em ``docs/system_design/11-WORKFLOW-DESENVOLVIMENTO.md``.

Regras críticas (Pilar 11 + Princípio 7):
- Nenhuma leitura de ``DEILE_OTLP_*`` aqui — config via ``get_observability_config()``.
  Única exceção: ``OTEL_METRIC_EXPORT_INTERVAL`` (var OTel-padrão, D4).
- Falha silenciosa: todo ``record_*`` tem ``try/except Exception``.
- Labels são strings bounded de enum fechado — sem ``task_id``/``session_id``/
  ``sha``/``branch``/``pr``/``model``/``error_code`` (Pilar 08 / Decisão #42).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, FrozenSet, Optional

from deile.observability.config import get_observability_config
from deile.observability.dispatch_schema import (
    ATTR_POD,
    ATTR_ROLE,
    ATTR_SCHEMA_VERSION,
    SCHEMA_VERSION,
    get_pod_metadata,
)

__all__ = [
    "record_dispatch_total",
    "record_dispatch_failed_total",
    "record_dispatch_duration_ms",
    "record_dispatch_tool_burst_total",
    "record_git_push_total",
    "record_forge_pr_review_total",
    "_tool_burst_bucket",
    "shutdown_dispatch_metrics",
    "reset_dispatch_metrics",
    "metrics_available",
]

_logger = logging.getLogger("deile.dispatch")

# ── instrument names ──────────────────────────────────────────────────────

METRIC_DISPATCH_TOTAL = "deile.dispatch.total"
METRIC_DISPATCH_FAILED_TOTAL = "deile.dispatch.failed.total"
METRIC_DISPATCH_DURATION_MS = "deile.dispatch.duration_ms"
METRIC_DISPATCH_TOOL_BURST_TOTAL = "deile.dispatch.tool_burst.total"
METRIC_DISPATCH_OTLP_DROP_TOTAL = "deile.dispatch.otlp_drop.total"
METRIC_FORGE_PR_REVIEW_TOTAL = "deile.forge.pr_review.total"
METRIC_GIT_PUSH_TOTAL = "deile.git.push.total"

# ── allowed labels (D2 — cardinality bounded) ──────────────────────────────

_ALLOWED_LABELS: Dict[str, FrozenSet[str]] = {
    METRIC_DISPATCH_TOTAL: frozenset({"role", "outcome"}),
    METRIC_DISPATCH_FAILED_TOTAL: frozenset({"role", "reason"}),
    METRIC_DISPATCH_DURATION_MS: frozenset({"role", "outcome"}),
    METRIC_DISPATCH_TOOL_BURST_TOTAL: frozenset({"role", "bucket"}),
    METRIC_DISPATCH_OTLP_DROP_TOTAL: frozenset({"reason"}),
    METRIC_FORGE_PR_REVIEW_TOTAL: frozenset({"decision"}),
    METRIC_GIT_PUSH_TOTAL: frozenset({"outcome"}),
}

# Labels proibidas (alta cardinalidade / segredo) — verificadas em AC3.
# Pertencem a span attributes (#443), nunca a metric labels.

_BUCKET_NOTE = (
    "label '500+' inicia em count=100, não 500 — preservado para compat "
    "futura de dashboard"
)


def _tool_burst_bucket(count: int) -> str:
    """Mapeia o número de tools no burst a um bucket de cardinality fechada.

    Range: ``<50`` → ``'50-'``, ``50-99`` → ``'100-'``, ``≥100`` → ``'500+'``.
    Ver :data:`_BUCKET_NOTE` — o label ``'500+'`` é preservado para compat de
    dashboard mas o range real inicia em 100.
    """
    if count < 50:
        return "50-"
    if count < 100:
        return "100-"
    return "500+"


def _validate_labels(metric_name: str, labels: Dict[str, Any]) -> None:
    """Raise ``ValueError`` se alguma label key estiver fora do set fechado."""
    allowed = _ALLOWED_LABELS[metric_name]
    for key in labels:
        if key not in allowed:
            raise ValueError(f"label '{key}' not allowed for metric '{metric_name}'")


# ── SDK availability ────────────────────────────────────────────────────────


def metrics_available() -> bool:
    """Retorna True se a Metrics API/SDK do OpenTelemetry está disponível."""
    try:
        import opentelemetry.metrics  # noqa: F401  pylint: disable=import-outside-toplevel
        import opentelemetry.sdk.metrics  # noqa: F401  pylint: disable=import-outside-toplevel

        return True
    except ImportError:
        return False


# ── SDK unavailable warning (once per process, lazy — D5) ────────────────────

_sdk_warned = False
_sdk_warned_lock = threading.Lock()


def _warn_sdk_unavailable() -> None:
    global _sdk_warned
    with _sdk_warned_lock:
        if not _sdk_warned:
            _logger.info("dispatch_metrics: otel_sdk_available=false sink=disabled")
            _sdk_warned = True


# ── drop counter (independente de traces/logs, D5) ──────────────────────────

_drop_counter: int = 0
_last_drop_log_ts: float = 0.0
_drop_lock = threading.Lock()
_time_fn = time.monotonic  # substituível em testes
_DROP_THROTTLE_S = 60.0


def _record_drop(reason: str) -> None:
    """Acumula drops e emite linha ≤1×/60s, incrementando otlp_drop counter.

    O contador local dá visibilidade mesmo quando o próprio sink de métricas
    está falhando; ``deile.dispatch.otlp_drop.total`` registra o agregado no
    flush (best-effort, isolado).
    """
    global _drop_counter, _last_drop_log_ts
    flushed = 0
    flush_reason = reason
    with _drop_lock:
        now = _time_fn()
        if now - _last_drop_log_ts >= _DROP_THROTTLE_S and _drop_counter > 0:
            flushed = _drop_counter
            _logger.info(
                "dispatch.otlp_metric_drop count=%d reason=%s",
                _drop_counter,
                reason,
            )
            _drop_counter = 0
            _last_drop_log_ts = now
        _drop_counter += 1
    if flushed > 0:
        _emit_drop_metric(flush_reason, flushed)


def _emit_drop_metric(reason: str, amount: int) -> None:
    """Incrementa ``deile.dispatch.otlp_drop.total`` (best-effort, isolado)."""
    try:
        instrument = _get_instrument(METRIC_DISPATCH_OTLP_DROP_TOTAL)
        if instrument is None:
            return
        instrument.add(amount, attributes={"reason": str(reason)})
    except Exception:  # noqa: BLE001 — drop metric nunca propaga
        pass


# ── MeterProvider singleton (D1, D7) ─────────────────────────────────────────

_meter_provider_singleton: Optional[Any] = None
# True once init has been attempted (distinguishes "not tried" from "tried+off").
_provider_tried: bool = False
_provider_lock = threading.Lock()

# Exactly 1 on successful init — used by idempotency tests (D7/AC9).
_init_count: int = 0

# Instrumentos criados uma vez (D2).
_instruments: Dict[str, Any] = {}

# Marcador para monkeypatch em testes — fica None em produção.
_meter_provider: Any = None


def _module_injected_provider() -> Any:
    """Retorna ``_meter_provider`` do módulo (monkeypatch por testes), ou None."""
    return globals().get("_meter_provider", None)


def _get_dispatch_meter_provider() -> Optional[Any]:
    """Retorna o ``MeterProvider`` singleton (OTLP real ou None quando off).

    Lazy-init na primeira chamada (D1, D6). Thread-safe e idempotente (D7).
    Emite uma linha INFO na primeira chamada se SDK ausente (D5).
    """
    global _meter_provider_singleton, _provider_tried, _init_count

    if _provider_tried:
        return _meter_provider_singleton

    with _provider_lock:
        if _provider_tried:
            return _meter_provider_singleton

        config = get_observability_config()

        # Kill-switch global, endpoint vazio ou kill-switch isolado → no metrics.
        if not config.is_enabled or config.metrics_disabled:
            _provider_tried = True
            return None

        # Provider injetado por teste (monkeypatch — D7).
        injected = _module_injected_provider()
        if injected is not None:
            _meter_provider_singleton = injected
            _init_instruments_from_provider(injected)
            _init_count += 1
            _provider_tried = True
            return _meter_provider_singleton

        if not metrics_available():
            _warn_sdk_unavailable()
            _provider_tried = True
            return None

        try:
            _meter_provider_singleton = _build_meter_provider(config)
            _init_instruments_from_provider(_meter_provider_singleton)
            _init_count += 1
        except Exception as exc:  # noqa: BLE001 — fail open
            _logger.warning(
                "dispatch_metrics: MeterProvider setup failed (%s); sink=disabled",
                exc,
            )
            _meter_provider_singleton = None
        finally:
            _provider_tried = True

        return _meter_provider_singleton


def _build_meter_provider(config: Any) -> Any:
    """Constrói o ``MeterProvider`` apontando para o collector OTLP (D1)."""
    from opentelemetry.sdk.metrics import (  # pylint: disable=import-outside-toplevel
        MeterProvider,
    )
    from opentelemetry.sdk.resources import (  # pylint: disable=import-outside-toplevel
        SERVICE_NAME,
        Resource,
    )

    pod = get_pod_metadata()
    resource = Resource.create(
        {
            SERVICE_NAME: config.service_name,
            ATTR_ROLE: pod["role"],
            ATTR_POD: pod["pod"],
            ATTR_SCHEMA_VERSION: SCHEMA_VERSION,
        }
    )
    reader = _make_reader(config)
    readers = [reader] if reader is not None else []
    return MeterProvider(resource=resource, metric_readers=readers)


def _export_interval_ms() -> int:
    """Lê ``OTEL_METRIC_EXPORT_INTERVAL`` (var OTel-padrão).

    Exceção documentada ao Princípio 7: é var OTel-padrão (não DEILE-específica);
    adicionar campo a ``ObservabilityConfig`` para um único parâmetro de reader
    seria mais invasivo que o benefício. Inválido → default 60000.
    """
    # OTEL standard env var — exception to Principle 7 (not DEILE-specific).
    raw = os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "60000")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 60000


def _make_reader(config: Any) -> Optional[Any]:
    """Constrói ``PeriodicExportingMetricReader`` + ``OTLPMetricExporter`` (D4).

    Passa :func:`_export_interval_ms` como ``export_interval_millis``.
    Falha silenciosa → None.
    """
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # pylint: disable=import-outside-toplevel
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics.export import (  # pylint: disable=import-outside-toplevel
            PeriodicExportingMetricReader,
        )
    except ImportError as exc:
        _logger.warning(
            "dispatch_metrics: OTLPMetricExporter não disponível (%s); "
            "métricas não exportadas.",
            exc,
        )
        return None
    try:
        exporter = OTLPMetricExporter(
            endpoint=config.endpoint,
            insecure=config.insecure,
            headers=config.headers or None,
        )
        return PeriodicExportingMetricReader(
            exporter, export_interval_millis=_export_interval_ms()
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("dispatch_metrics: reader init failed: %s", exc)
        return None


def _init_instruments_from_provider(provider: Any) -> None:
    """Cria os 7 instruments uma vez a partir do meter do provider (D2)."""
    global _instruments
    if _instruments:
        return
    meter = provider.get_meter("deile.dispatch")
    _instruments = {
        METRIC_DISPATCH_TOTAL: meter.create_counter(
            name=METRIC_DISPATCH_TOTAL,
            description="Dispatches por role/outcome (completed|failed).",
            unit="1",
        ),
        METRIC_DISPATCH_FAILED_TOTAL: meter.create_counter(
            name=METRIC_DISPATCH_FAILED_TOTAL,
            description="Dispatches falhos por role/reason (enum fechado de #435).",
            unit="1",
        ),
        METRIC_DISPATCH_DURATION_MS: meter.create_histogram(
            name=METRIC_DISPATCH_DURATION_MS,
            description="Duração do dispatch por role/outcome (p50/p95/p99).",
            unit="ms",
        ),
        METRIC_DISPATCH_TOOL_BURST_TOTAL: meter.create_counter(
            name=METRIC_DISPATCH_TOOL_BURST_TOTAL,
            description="Tool bursts por role/bucket (ver _tool_burst_bucket).",
            unit="1",
        ),
        METRIC_DISPATCH_OTLP_DROP_TOTAL: meter.create_counter(
            name=METRIC_DISPATCH_OTLP_DROP_TOTAL,
            description="Pontos de métrica perdidos por reason (export_error etc).",
            unit="1",
        ),
        METRIC_FORGE_PR_REVIEW_TOTAL: meter.create_counter(
            name=METRIC_FORGE_PR_REVIEW_TOTAL,
            description="Reviews de PR por decision (APPROVED|CHANGES_REQUESTED|COMMENTED).",
            unit="1",
        ),
        METRIC_GIT_PUSH_TOTAL: meter.create_counter(
            name=METRIC_GIT_PUSH_TOTAL,
            description="Git pushes por outcome (ok|fail).",
            unit="1",
        ),
    }


def _get_instrument(metric_name: str) -> Optional[Any]:
    """Retorna o instrument do meter (lazy-init do provider). None se off."""
    if _get_dispatch_meter_provider() is None:
        return None
    return _instruments.get(metric_name)


# ── record helpers (um por métrica, D2/D3) ──────────────────────────────────


def _add(metric_name: str, value: float, labels: Dict[str, Any]) -> None:
    """Common path para counters: valida labels, ensure provider, ``add``.

    Validação de label (cardinality bound) roda SEMPRE — mesmo com sink off —
    para que o contrato de allowlist falhe rápido (AC2). A emissão em si é
    best-effort e nunca propaga (Pilar 11).
    """
    _validate_labels(metric_name, labels)
    try:
        instrument = _get_instrument(metric_name)
        if instrument is None:
            return
        instrument.add(value, attributes=labels)
    except Exception:  # noqa: BLE001 — métricas nunca quebram o turn
        _record_drop("emit_error")


def _record(metric_name: str, value: float, labels: Dict[str, Any]) -> None:
    """Common path para histograms: valida labels, ensure provider, ``record``."""
    _validate_labels(metric_name, labels)
    try:
        instrument = _get_instrument(metric_name)
        if instrument is None:
            return
        instrument.record(value, attributes=labels)
    except Exception:  # noqa: BLE001 — métricas nunca quebram o turn
        _record_drop("emit_error")


def record_dispatch_total(*, role: str, outcome: str, **extra: Any) -> None:
    """Incrementa ``deile.dispatch.total{role,outcome}``."""
    _add(
        METRIC_DISPATCH_TOTAL, 1, {"role": str(role), "outcome": str(outcome), **extra}
    )


def record_dispatch_failed_total(*, role: str, reason: str, **extra: Any) -> None:
    """Incrementa ``deile.dispatch.failed.total{role,reason}``."""
    _add(
        METRIC_DISPATCH_FAILED_TOTAL,
        1,
        {"role": str(role), "reason": str(reason), **extra},
    )


def record_dispatch_duration_ms(
    *, role: str, outcome: str, value_ms: float, **extra: Any
) -> None:
    """Registra ``deile.dispatch.duration_ms{role,outcome}``."""
    _record(
        METRIC_DISPATCH_DURATION_MS,
        float(value_ms),
        {"role": str(role), "outcome": str(outcome), **extra},
    )


def record_dispatch_tool_burst_total(*, role: str, bucket: str, **extra: Any) -> None:
    """Incrementa ``deile.dispatch.tool_burst.total{role,bucket}``."""
    _add(
        METRIC_DISPATCH_TOOL_BURST_TOTAL,
        1,
        {"role": str(role), "bucket": str(bucket), **extra},
    )


def record_git_push_total(*, outcome: str, **extra: Any) -> None:
    """Incrementa ``deile.git.push.total{outcome}``."""
    _add(METRIC_GIT_PUSH_TOTAL, 1, {"outcome": str(outcome), **extra})


def record_forge_pr_review_total(*, decision: str, **extra: Any) -> None:
    """Incrementa ``deile.forge.pr_review.total{decision}``."""
    _add(METRIC_FORGE_PR_REVIEW_TOTAL, 1, {"decision": str(decision), **extra})


# ── lifecycle (D7) ───────────────────────────────────────────────────────────


def shutdown_dispatch_metrics() -> None:
    """Flush + shutdown do MeterProvider (evita threads órfãs em testes)."""
    provider = _meter_provider_singleton
    if provider is None:
        return
    try:
        shutdown_fn = getattr(provider, "shutdown", None)
        if callable(shutdown_fn):
            shutdown_fn()
    except Exception:  # noqa: BLE001
        pass


def reset_dispatch_metrics() -> None:
    """Reseta todos os singletons — apenas para testes."""
    global _meter_provider_singleton, _provider_tried, _init_count
    global _instruments, _drop_counter, _last_drop_log_ts, _sdk_warned

    with _provider_lock:
        if _meter_provider_singleton is not None:
            try:
                shutdown_fn = getattr(_meter_provider_singleton, "shutdown", None)
                if callable(shutdown_fn):
                    shutdown_fn()
            except Exception:  # noqa: BLE001
                pass
            _meter_provider_singleton = None
        _provider_tried = False
        _init_count = 0
        _instruments = {}

    with _drop_lock:
        _drop_counter = 0
        _last_drop_log_ts = 0.0

    with _sdk_warned_lock:
        _sdk_warned = False
