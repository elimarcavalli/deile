"""Pipeline de log records correlacionados (Loki) — issue #454.

Para cada evento que ``dispatch_export`` emite como span event (dispatch.*)
ou como child span (git.*/forge.*), este módulo emite um ``LogRecord`` OTel
correlacionado via ``trace_id``/``span_id``. O Collector roteia para Loki
(logs) enquanto o mesmo dado flui para Tempo (traces) pelo pipeline de traces.

Decisões de arquitetura (D1-D8 da issue #454):

- **D1**: ``LoggerProvider`` PARALELO ao ``TracerProvider`` (não compartilhado).
  Mesma config via ``get_observability_config()``. Resource attrs idênticos:
  ``service.name``, ``deile.role``, ``deile.pod``,
  ``deile.dispatch.schema_version="1.0.0"``.
- **D2**: Kill-switch isolado ``DEILE_OTLP_LOGS_DISABLED`` — spans continuam
  emitindo; logs param. Não afeta ``DEILE_OBSERVABILITY_DISABLED``.
- **D3**: ``trace_id``/``span_id`` capturados via
  ``get_current_span().get_span_context()`` ANTES de qualquer mutação do span.
  ``body_for()`` produz a string wire idêntica à que ``dispatch_logger`` escreve
  em stdout.
- **D4**: Severity matrix (primeira regra que casa wins):
  ``dispatch.failed reason=auth_expired`` → ERROR/17;
  ``dispatch.failed`` outro reason → WARN/13;
  ``dispatch.tool_burst count>50`` → WARN/13;
  ``dispatch.tool_burst count<=50`` → INFO/9;
  ``git.*``/``forge.*`` com status=fail/error → WARN/13;
  todos os outros → INFO/9.
- **D5**: Redact aplicado em body e attrs. Drop counter separado do de traces;
  log ``dispatch.otlp_log_drop`` ≤1×/60s. Failure isolation: try/except
  separados para span e log em dispatch_export.
- **D6**: Sem novas dependências. ``opentelemetry-api>=1.27.0`` + SDK >= 1.27
  já em ``pyproject.toml``. Graceful no-op quando SDK ausente. Linha INFO única
  na primeira chamada a ``get_log_provider()`` (lazy — não emitida em module
  import, consistente com D1).
- **D7**: Thread-safe via ``threading.Lock``. Idempotente:
  ``get_log_provider()`` retorna sempre o mesmo objeto. ``_init_count`` exatamente
  1 após primeira init bem-sucedida.
- **D8**: Documentado em ``docs/system_design/11-WORKFLOW-DESENVOLVIMENTO.md``.

Regras críticas:
- Nenhuma leitura direta de env — config via ``get_observability_config()``.
- Falha silenciosa: todo emit tem ``try/except Exception``.
- Redact aplicado ANTES de qualquer emit.
- Drop counter thread-safe + log ≤1×/60s.
"""

from __future__ import annotations

import logging
import re as _re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from deile.observability.config import get_observability_config
from deile.observability.dispatch_schema import (
    ATTR_POD,
    ATTR_ROLE,
    ATTR_SCHEMA_VERSION,
    SCHEMA_VERSION,
    get_pod_metadata,
)

__all__ = [
    "get_log_provider",
    "get_dispatch_log_export",
    "emit_log_record",
    "body_for",
    "reset_dispatch_log_export",
    # legacy alias used in some tests
    "emit_log_record",
]

_logger = logging.getLogger(__name__)

# ── redaction (mirrors dispatch_export._REDACT_RE — same regex) ──────────────

_REDACT_RE = _re.compile(
    r"(ghp_[A-Za-z0-9]{36,}|glpat-[A-Za-z0-9_-]{20,}|gldt-[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|Bearer\s+\S{10,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[A-Z0-9]{16,}|[A-Za-z0-9+/]{40,}={0,2})",
    _re.ASCII,
)


def _redact(value: str) -> str:
    """Substitui padrões de token/segredo por ``[REDACTED]``."""
    return _REDACT_RE.sub("[REDACTED]", value)


def _safe_attrs(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica redact em todos os valores string do dict."""
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        out[k] = _redact(str(v)) if isinstance(v, str) else v
    return out


# ── severity matrix (D4 — first-match wins) ──────────────────────────────────
# OTel SeverityNumber: INFO=9, WARN=13, ERROR=17 (per OTLP spec)

_SEV_INFO: Tuple[str, int] = ("INFO", 9)
_SEV_WARN: Tuple[str, int] = ("WARN", 13)
_SEV_ERROR: Tuple[str, int] = ("ERROR", 17)


def _severity_for(event_name: str, attrs: Dict[str, Any]) -> Tuple[str, int]:
    """Retorna (severity_text, severity_number). Regras top-down, primeira que casa wins."""
    # Rule 1: dispatch.failed reason=auth_expired → ERROR/17
    if event_name == "dispatch.failed":
        reason = str(attrs.get("deile.dispatch.reason", ""))
        if reason == "auth_expired":
            return _SEV_ERROR
        # Rule 2: dispatch.failed any other reason → WARN/13
        return _SEV_WARN
    # Rule 3: dispatch.tool_burst count>50 → WARN/13
    if event_name == "dispatch.tool_burst":
        count = attrs.get("deile.dispatch.tool_count", 0)
        try:
            if int(count) > 50:
                return _SEV_WARN
        except (TypeError, ValueError):
            pass
        # Rule 4: dispatch.tool_burst count<=50 → INFO/9
        return _SEV_INFO
    # Rule 5: git.*/forge.* with outcome/status=fail/error → WARN/13
    if event_name.startswith(("git.", "forge.")):
        git_status = str(attrs.get("deile.git.status", "")).lower()
        forge_status = str(attrs.get("deile.forge.status", "")).lower()
        status = git_status or forge_status
        if status in ("fail", "error", "failed"):
            return _SEV_WARN
        return _SEV_INFO
    # Rule 6: all others → INFO/9
    return _SEV_INFO


# ── body_for (D3) ─────────────────────────────────────────────────────────────


def body_for(event_name: str, attrs: Dict[str, Any]) -> str:
    """Produz a string wire do log record.

    Formato: ``<event_name> <key>=<value> ...`` (pares ordenados por key).
    Idêntico ao formato que ``dispatch_logger`` escreve em stdout.
    Redact aplicado em todos os valores string.
    """
    parts = [event_name]
    for k in sorted(attrs.keys()):
        v = attrs[k]
        safe_v = _redact(str(v)) if isinstance(v, str) else str(v)
        parts.append(f"{k}={safe_v}")
    return " ".join(parts)


# ── drop counter (independente do de traces, D5) ──────────────────────────────

_log_drop_counter: int = 0
_log_last_drop_log_ts: float = 0.0
_log_drop_lock = threading.Lock()
_log_time_fn = time.monotonic  # substituível em testes
_LOG_DROP_THROTTLE_S = 60.0


def _record_log_drop(reason: str) -> None:
    """Incrementa o contador de drops de log e emite linha ≤1×/60s."""
    global _log_drop_counter, _log_last_drop_log_ts
    with _log_drop_lock:
        now = _log_time_fn()
        if (
            now - _log_last_drop_log_ts >= _LOG_DROP_THROTTLE_S
            and _log_drop_counter > 0
        ):
            _logger.info(
                "dispatch.otlp_log_drop count=%d reason=%s",
                _log_drop_counter,
                reason,
            )
            _log_drop_counter = 0
            _log_last_drop_log_ts = now
        _log_drop_counter += 1


# ── SDK availability check ────────────────────────────────────────────────────


def otel_logs_available() -> bool:
    """Retorna True se a Logs API/SDK do OpenTelemetry está disponível."""
    try:
        import opentelemetry._logs  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel
        import opentelemetry.sdk._logs  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel

        return True
    except ImportError:
        return False


# ── SDK unavailable warning (once per process, lazy — D6) ─────────────────────

_sdk_warned = False
_sdk_warned_lock = threading.Lock()


def _warn_sdk_unavailable() -> None:
    global _sdk_warned
    with _sdk_warned_lock:
        if not _sdk_warned:
            _logger.info("dispatch_log_export: otel_sdk_available=false sink=disabled")
            _sdk_warned = True


# ── LoggerProvider singleton (D1, D7) ─────────────────────────────────────────

_log_provider_singleton: Optional[Any] = None
# True once we have attempted init (distinguishes "not tried" from "tried+failed/disabled")
_log_provider_tried: bool = False
_log_provider_lock = threading.Lock()

# Exactly 1 on successful init — used by idempotency tests (D7)
_init_count: int = 0


def _module_injected_log_provider() -> Any:
    """Retorna ``_log_provider`` do módulo (monkeypatch por testes), ou None."""
    return globals().get("_log_provider", None)


# Marcador para monkeypatch em testes — fica None em produção.
_log_provider: Any = None


def get_log_provider() -> Optional[Any]:
    """Retorna o LoggerProvider singleton (OTLP real ou None quando desligado).

    Lazy-init na primeira chamada (D1, D6). Thread-safe (D7). Idempotente.
    Emite uma linha INFO na primeira chamada se SDK ausente (D6 — não emitida
    em module import).
    """
    global _log_provider_singleton, _log_provider_tried, _init_count

    # Fast path — already decided
    if _log_provider_tried:
        return _log_provider_singleton

    with _log_provider_lock:
        if _log_provider_tried:
            return _log_provider_singleton

        config = get_observability_config()

        # Kill-switch global (disabled) or endpoint empty → no logs
        if not config.is_enabled or config.logs_disabled:
            _log_provider_tried = True
            return None

        # Check for test-injected provider (monkeypatch pattern, D7)
        injected = _module_injected_log_provider()
        if injected is not None:
            _log_provider_singleton = injected
            _init_count += 1
            _log_provider_tried = True
            return _log_provider_singleton

        # SDK check — lazy warn on first miss (D6)
        if not otel_logs_available():
            _warn_sdk_unavailable()
            _log_provider_tried = True
            return None

        try:
            _log_provider_singleton = _build_log_provider(config)
            _init_count += 1
        except Exception as exc:  # noqa: BLE001 — fail open
            _logger.warning(
                "dispatch_log_export: LoggerProvider setup failed (%s); sink=disabled",
                exc,
            )
            _log_provider_singleton = None
        finally:
            _log_provider_tried = True

        return _log_provider_singleton


def _build_log_provider(config: Any) -> Any:
    """Constrói o LoggerProvider apontando para o collector OTLP (D1)."""
    from opentelemetry.sdk._logs import (  # pylint: disable=import-outside-toplevel
        LoggerProvider,
    )
    from opentelemetry.sdk._logs.export import (  # pylint: disable=import-outside-toplevel
        BatchLogRecordProcessor,
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
    provider = LoggerProvider(resource=resource)
    exporter = _make_log_exporter(config)
    if exporter is not None:
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return provider


def _make_log_exporter(config: Any) -> Optional[Any]:
    """Constrói o OTLPLogExporter (gRPC). Falha silenciosa → None."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # pylint: disable=import-outside-toplevel
            OTLPLogExporter,
        )
    except ImportError as exc:
        _logger.warning(
            "dispatch_log_export: OTLPLogExporter não disponível (%s); "
            "log records acumulados em memória e perdidos.",
            exc,
        )
        return None
    try:
        return OTLPLogExporter(
            endpoint=config.endpoint,
            insecure=config.insecure,
            headers=config.headers or None,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("dispatch_log_export: OTLPLogExporter init failed: %s", exc)
        return None


# ── DispatchLogExport — thin facade (stateless) ───────────────────────────────


class DispatchLogExport:
    """Thin facade sobre ``get_log_provider()`` — stateless, thread-safe."""

    def emit(
        self,
        event_name: str,
        attrs: Dict[str, Any],
        trace_id: int = 0,
        span_id: int = 0,
        trace_flags: int = 0,
    ) -> None:
        """Emite um LogRecord para o evento dado.

        Args:
            event_name: nome do evento (e.g. ``dispatch.received``).
            attrs: atributos do evento (valores já passados por _safe_attrs
                pelo caller, mas redact é aplicado novamente para segurança).
            trace_id: trace_id do span ativo (int 128-bit).
            span_id: span_id do span ativo (int 64-bit).
            trace_flags: trace flags do SpanContext (int).
        """
        emit_log_record(
            event_name=event_name,
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=trace_flags,
            attributes=attrs,
        )


# ── module-level singleton for DispatchLogExport (D7) ────────────────────────

_dispatch_log_export_singleton: Optional[DispatchLogExport] = None
_dispatch_log_export_lock = threading.Lock()


def get_dispatch_log_export() -> DispatchLogExport:
    """Retorna o singleton de DispatchLogExport (thread-safe, idempotente)."""
    global _dispatch_log_export_singleton
    if _dispatch_log_export_singleton is not None:
        return _dispatch_log_export_singleton
    with _dispatch_log_export_lock:
        if _dispatch_log_export_singleton is None:
            _dispatch_log_export_singleton = DispatchLogExport()
        return _dispatch_log_export_singleton


# ── emit_log_record — public entry point ──────────────────────────────────────


def emit_log_record(
    event_name: str,
    trace_id: int,
    span_id: int,
    trace_flags: Any,
    attributes: Dict[str, Any],
) -> None:
    """Emite um LogRecord correlacionado para um evento dispatch/git/forge.

    Failure isolation (D5): qualquer exceção interna é capturada aqui — nunca
    propaga para o caller (span pipeline).

    Args:
        event_name: nome do evento OTel (e.g. ``dispatch.received``).
        trace_id: trace_id inteiro do span associado (128-bit).
        span_id: span_id inteiro do span associado (64-bit).
        trace_flags: TraceFlags do SpanContext (ou int).
        attributes: atributos do evento.
    """
    try:
        provider = get_log_provider()
        if provider is None:
            return

        from opentelemetry._logs import (  # pylint: disable=import-outside-toplevel
            LogRecord,
            SeverityNumber,
        )

        severity_text, severity_number = _severity_for(event_name, attributes)
        safe = _safe_attrs(attributes)
        body = body_for(event_name, safe)

        # Snapshot complete — no TOCTOU possible after this point (D3/D5).
        record = LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=int(trace_flags) if trace_flags is not None else 0,
            severity_text=severity_text,
            severity_number=SeverityNumber(severity_number),
            body=body,
            attributes=safe,
        )

        log_logger = provider.get_logger("deile.dispatch")
        log_logger.emit(record)

    except Exception:  # noqa: BLE001 — log errors never affect span pipeline (D5)
        _record_log_drop("emit_error")


# ── reset helper (tests only) ─────────────────────────────────────────────────


def reset_dispatch_log_export() -> None:
    """Reseta todos os singletons — apenas para testes."""
    global _log_provider_singleton, _log_provider_tried, _init_count
    global _log_drop_counter, _log_last_drop_log_ts, _sdk_warned
    global _dispatch_log_export_singleton

    with _log_provider_lock:
        if _log_provider_singleton is not None:
            try:
                shutdown_fn = getattr(_log_provider_singleton, "shutdown", None)
                if callable(shutdown_fn):
                    shutdown_fn()
            except Exception:  # noqa: BLE001
                pass
            _log_provider_singleton = None
        _log_provider_tried = False
        _init_count = 0

    with _log_drop_lock:
        _log_drop_counter = 0
        _log_last_drop_log_ts = 0.0

    with _sdk_warned_lock:
        _sdk_warned = False

    with _dispatch_log_export_lock:
        _dispatch_log_export_singleton = None
