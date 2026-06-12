"""Adapter OTLP-traces para eventos dispatch.*/git.*/forge.* — Decisão #47.

Cada chamada do ``infra/k8s/dispatch_logger`` emite OTLP traces em paralelo
à linha textual no stdout. Este módulo é o ponto único de integração: fornece
``emit_*`` que o dispatch_logger chama via hook único.

Sinal V1: **traces apenas** (logs/metrics nas sub-issues #454/#455).

Mapeamento evento → forma OTLP (Decisão #47 / D2):

- ``dispatch.received``  → abre root span ``deile.dispatch`` + span event
- ``dispatch.*`` (middle) → span events no root span
- ``dispatch.completed``  → span event + ``set_status(OK)`` + ``end()``
- ``dispatch.failed``     → span event + ``set_status(ERROR)`` + ``end()``
- ``git.*``/``forge.*``   → child spans curtos no contexto do root

Regras críticas:
- Nenhuma leitura direta de env neste módulo — config via ``get_observability_config()``.
- Falha silenciosa: todo ``emit_*`` tem ``try/except Exception``.
- Redact de valores sensíveis antes de qualquer ``set_attribute``.
- Drop counter thread-safe + log ``dispatch.otlp_drop`` ≤1×/60s.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import deile.observability.dispatch_metrics as dispatch_metrics
from deile.observability.config import get_observability_config
from deile.observability.dispatch_log_export import emit_log_record
from deile.observability.dispatch_schema import (ATTR_POD, ATTR_ROLE,
                                                 ATTR_SCHEMA_VERSION,
                                                 SCHEMA_VERSION,
                                                 DispatchCompletedAttrs,
                                                 DispatchFailedAttrs,
                                                 DispatchModelResolvedAttrs,
                                                 DispatchProgressAttrs,
                                                 DispatchReceivedAttrs,
                                                 DispatchToolBurstAttrs,
                                                 ForgePrOpenAttrs,
                                                 ForgePrReviewAttrs,
                                                 GitCommitAttrs, GitPushAttrs,
                                                 get_pod_metadata)
from deile.observability.semconv_mapping import apply_semconv_attrs
from deile.observability.tracer import OtlpTracer, get_tracer, otel_available

__all__ = [
    "emit_dispatch_received",
    "emit_dispatch_model_resolved",
    "emit_dispatch_progress",
    "emit_dispatch_tool_burst",
    "emit_dispatch_completed",
    "emit_dispatch_failed",
    "emit_git_commit",
    "emit_git_push",
    "emit_forge_pr_open",
    "emit_forge_pr_review",
    "reset_dispatch_export",
]

_logger = logging.getLogger(__name__)

# ── redaction ─────────────────────────────────────────────────────────────

_REDACT_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{36,}|glpat-[A-Za-z0-9_-]{20,}|gldt-[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|Bearer\s+\S{10,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[A-Z0-9]{16,}|[A-Za-z0-9+/]{40,}={0,2})",
    re.ASCII,
)


def _redact(value: str) -> str:
    """Substitui padrões de token/segredo por ``[REDACTED]``."""
    return _REDACT_RE.sub("[REDACTED]", value)


def _safe_str(value: Any) -> str:
    """Converte para str com redact aplicado."""
    return _redact(str(value) if value is not None else "")


def _safe_attrs(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica redact em todos os valores string do dict."""
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        out[k] = _redact(str(v)) if isinstance(v, str) else v
    return out


# ── drop counter ──────────────────────────────────────────────────────────

_drop_counter: int = 0
_last_drop_log_ts: float = 0.0
_drop_lock = threading.Lock()
_time_fn = time.monotonic  # substituível em testes
_DROP_THROTTLE_S = 60.0


def _record_drop(reason: str) -> None:
    """Incrementa o contador de drops e emite log ≤1×/60s.

    O log é emitido ANTES de incrementar o drop atual, de modo que o count
    reflete os drops acumulados no período anterior. O drop corrente é
    adicionado ao próximo período.
    """
    global _drop_counter, _last_drop_log_ts
    with _drop_lock:
        now = _time_fn()
        if now - _last_drop_log_ts >= _DROP_THROTTLE_S and _drop_counter > 0:
            _logger.info(
                "dispatch.otlp_drop count=%d reason=%s", _drop_counter, reason
            )
            _drop_counter = 0
            _last_drop_log_ts = now
        _drop_counter += 1


# ── SDK unavailable warning (once per process) ─────────────────────────────

_sdk_warned = False
_sdk_warned_lock = threading.Lock()


def _warn_sdk_unavailable() -> None:
    global _sdk_warned
    with _sdk_warned_lock:
        if not _sdk_warned:
            _logger.info("dispatch_export: otel_sdk_available=false sink=disabled")
            _sdk_warned = True


# ── raw tracer access ────────────────────────────────────────────────────

def _get_raw_tracer() -> Optional[Any]:
    """Retorna o tracer OTel SDK bruto se OTLP estiver habilitado, else None."""
    config = get_observability_config()
    if not config.is_enabled:
        return None
    if not otel_available():
        _warn_sdk_unavailable()
        return None
    tracer = get_tracer()
    if isinstance(tracer, OtlpTracer):
        return tracer._ensure_provider()
    return None


# ── active root spans ────────────────────────────────────────────────────

_active_spans: Dict[str, Any] = {}
_spans_lock = threading.Lock()

# ── common span attrs (schema version + pod metadata) ─────────────────────


def _common_attrs() -> Dict[str, str]:
    pod = get_pod_metadata()
    return {
        ATTR_SCHEMA_VERSION: SCHEMA_VERSION,
        ATTR_ROLE: pod["role"],
        ATTR_POD: pod["pod"],
    }


def _role() -> str:
    """Role do pod (label de cardinality bounded para as métricas)."""
    return get_pod_metadata().get("role", "") or "unknown"


def _safe_record_metric(fn: Any, **kwargs: Any) -> None:
    """Chama um ``dispatch_metrics.record_*`` em try/except isolado (D3/D5).

    A fiação de métrica roda APÓS a operação de span; uma falha aqui nunca
    propaga para o pipeline de span/dispatch (observability best-effort).
    """
    try:
        fn(**kwargs)
    except Exception:  # noqa: BLE001 — metrics never break dispatch (D5)
        pass


# ── emit functions ───────────────────────────────────────────────────────

def emit_dispatch_received(
    task_id: str,
    session_id: str = "",
    model: str = "",
    branch: str = "",
) -> None:
    """Abre o root span ``deile.dispatch`` e registra o evento ``dispatch.received``."""
    schema = DispatchReceivedAttrs(
        task_id=_safe_str(task_id),
        session_id=_safe_str(session_id),
        model=_safe_str(model),
        branch=_safe_str(branch),
    )
    span_ctx = None
    try:
        raw = _get_raw_tracer()
        if raw is None:
            return
        attrs = {**schema.to_span_attrs(), **_common_attrs()}
        span = raw.start_span(DispatchReceivedAttrs.SPAN_NAME, attributes=attrs)
        span_ctx = span.get_span_context()
        span.add_event(DispatchReceivedAttrs.EVENT_NAME, attributes=_safe_attrs(schema.to_span_attrs()))
        with _spans_lock:
            existing = _active_spans.pop(task_id, None)
            if existing is not None:
                try:
                    existing.end()
                except Exception:  # noqa: BLE001
                    pass
            _active_spans[task_id] = span
    except Exception:  # noqa: BLE001 — fail open
        _record_drop("emit_error")
    _try_emit_log(span_ctx, DispatchReceivedAttrs.EVENT_NAME, _safe_attrs(schema.to_span_attrs()))


def emit_dispatch_model_resolved(
    task_id: str,
    model: str = "",
) -> None:
    """Registra o evento ``dispatch.model_resolved`` no root span."""
    schema = DispatchModelResolvedAttrs(model=_safe_str(model))
    span_ctx = None
    try:
        with _spans_lock:
            span = _active_spans.get(task_id)
        if span is None:
            return
        span_ctx = span.get_span_context()
        span.add_event(DispatchModelResolvedAttrs.EVENT_NAME, attributes=_safe_attrs(schema.to_event_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    _try_emit_log(span_ctx, DispatchModelResolvedAttrs.EVENT_NAME, _safe_attrs(schema.to_event_attrs()))


def emit_dispatch_progress(
    task_id: str,
    step: str = "",
    elapsed_s: float = 0.0,
) -> None:
    """Registra o evento ``dispatch.progress`` no root span."""
    schema = DispatchProgressAttrs(step=_safe_str(step), elapsed_s=float(elapsed_s))
    span_ctx = None
    try:
        with _spans_lock:
            span = _active_spans.get(task_id)
        if span is None:
            return
        span_ctx = span.get_span_context()
        span.add_event(DispatchProgressAttrs.EVENT_NAME, attributes=_safe_attrs(schema.to_event_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    _try_emit_log(span_ctx, DispatchProgressAttrs.EVENT_NAME, _safe_attrs(schema.to_event_attrs()))


def emit_dispatch_tool_burst(
    task_id: str,
    tools: str = "",
    count: int = 0,
) -> None:
    """Registra o evento ``dispatch.tool_burst`` no root span."""
    schema = DispatchToolBurstAttrs(tools=_safe_str(tools), count=int(count))
    span_ctx = None
    try:
        with _spans_lock:
            span = _active_spans.get(task_id)
        if span is None:
            return
        span_ctx = span.get_span_context()
        span.add_event(DispatchToolBurstAttrs.EVENT_NAME, attributes=_safe_attrs(schema.to_event_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    _try_emit_log(span_ctx, DispatchToolBurstAttrs.EVENT_NAME, _safe_attrs(schema.to_event_attrs()))
    # metrics hook (#455): tool burst bucketed por cardinality bounded.
    _safe_record_metric(
        dispatch_metrics.record_dispatch_tool_burst_total,
        role=_role(),
        bucket=dispatch_metrics._tool_burst_bucket(int(count)),
    )


def emit_dispatch_completed(
    task_id: str,
    elapsed_s: float = 0.0,
    outcome: str = "",
) -> None:
    """Fecha o root span com status OK."""
    schema = DispatchCompletedAttrs(elapsed_s=float(elapsed_s), outcome=_safe_str(outcome))
    span_ctx = None
    try:
        with _spans_lock:
            span = _active_spans.pop(task_id, None)
        if span is None:
            return
        from opentelemetry.trace import \
            StatusCode  # pylint: disable=import-outside-toplevel
        span_ctx = span.get_span_context()
        span.add_event(DispatchCompletedAttrs.EVENT_NAME, attributes=_safe_attrs(schema.to_event_attrs()))
        span.set_status(StatusCode.OK)
        span.end()
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    _try_emit_log(span_ctx, DispatchCompletedAttrs.EVENT_NAME, _safe_attrs(schema.to_event_attrs()))
    # metrics hook (#455): dispatch concluído → total + duração (elapsed_s → ms).
    role = _role()
    _safe_record_metric(
        dispatch_metrics.record_dispatch_total, role=role, outcome="completed"
    )
    _safe_record_metric(
        dispatch_metrics.record_dispatch_duration_ms,
        role=role,
        outcome="completed",
        value_ms=float(elapsed_s) * 1000.0,
    )


def emit_dispatch_failed(
    task_id: str,
    reason: str = "",
    elapsed_s: float = 0.0,
) -> None:
    """Fecha o root span com status ERROR."""
    schema = DispatchFailedAttrs(reason=_safe_str(reason), elapsed_s=float(elapsed_s))
    span_ctx = None
    try:
        with _spans_lock:
            span = _active_spans.pop(task_id, None)
        if span is None:
            return
        from opentelemetry.trace import \
            StatusCode  # pylint: disable=import-outside-toplevel
        span_ctx = span.get_span_context()
        span.add_event(DispatchFailedAttrs.EVENT_NAME, attributes=_safe_attrs(schema.to_event_attrs()))
        span.set_status(StatusCode.ERROR, description=_safe_str(reason))
        span.end()
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    _try_emit_log(span_ctx, DispatchFailedAttrs.EVENT_NAME, _safe_attrs(schema.to_event_attrs()))
    # metrics hook (#455): dispatch falho → total(failed) + failed(reason) + duração.
    role = _role()
    _safe_record_metric(
        dispatch_metrics.record_dispatch_total, role=role, outcome="failed"
    )
    _safe_record_metric(
        dispatch_metrics.record_dispatch_failed_total,
        role=role,
        reason=str(reason) if reason else "unknown",
    )
    _safe_record_metric(
        dispatch_metrics.record_dispatch_duration_ms,
        role=role,
        outcome="failed",
        value_ms=float(elapsed_s) * 1000.0,
    )


def _emit_child_span(task_id: str, name: str, attrs: Dict[str, Any]) -> None:
    """Cria e finaliza imediatamente um child span do root de task_id."""
    raw = _get_raw_tracer()
    if raw is None:
        return
    with _spans_lock:
        parent = _active_spans.get(task_id)
    if parent is None:
        return
    from opentelemetry.trace import \
        set_span_in_context  # pylint: disable=import-outside-toplevel
    ctx = set_span_in_context(parent)
    all_attrs = {**attrs, **_common_attrs()}
    child = raw.start_span(name, context=ctx, attributes=all_attrs)
    config = get_observability_config()
    if config.is_semconv_enabled:
        apply_semconv_attrs(child, attrs)
    child_ctx = child.get_span_context()
    child.end()
    _try_emit_log(child_ctx, name, attrs)


def emit_git_commit(
    task_id: str,
    repo: str = "",
    sha: str = "",
    status: str = "",
) -> None:
    """Emite child span ``git.commit`` no contexto do root span."""
    try:
        schema = GitCommitAttrs(repo=_safe_str(repo), sha=_safe_str(sha), status=_safe_str(status))
        _emit_child_span(task_id, GitCommitAttrs.SPAN_NAME, _safe_attrs(schema.to_span_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")


def emit_git_push(
    task_id: str,
    repo: str = "",
    branch: str = "",
    status: str = "",
) -> None:
    """Emite child span ``git.push`` no contexto do root span."""
    try:
        schema = GitPushAttrs(repo=_safe_str(repo), branch=_safe_str(branch), status=_safe_str(status))
        _emit_child_span(task_id, GitPushAttrs.SPAN_NAME, _safe_attrs(schema.to_span_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    # metrics hook (#455): push por outcome (status do schema carrega ok|fail).
    if status:
        _safe_record_metric(
            dispatch_metrics.record_git_push_total, outcome=str(status)
        )


def emit_forge_pr_open(
    task_id: str,
    repo: str = "",
    pr_number: int = 0,
    status: str = "",
) -> None:
    """Emite child span ``forge.pr_open`` no contexto do root span."""
    try:
        schema = ForgePrOpenAttrs(repo=_safe_str(repo), pr_number=int(pr_number), status=_safe_str(status))
        _emit_child_span(task_id, ForgePrOpenAttrs.SPAN_NAME, _safe_attrs(schema.to_span_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")


def emit_forge_pr_review(
    task_id: str,
    repo: str = "",
    pr_number: int = 0,
    status: str = "",
) -> None:
    """Emite child span ``forge.pr_review`` no contexto do root span."""
    try:
        schema = ForgePrReviewAttrs(repo=_safe_str(repo), pr_number=int(pr_number), status=_safe_str(status))
        _emit_child_span(task_id, ForgePrReviewAttrs.SPAN_NAME, _safe_attrs(schema.to_span_attrs()))
    except Exception:  # noqa: BLE001
        _record_drop("emit_error")
    # metrics hook (#455): review por decision (status do schema carrega a decisão).
    if status:
        _safe_record_metric(
            dispatch_metrics.record_forge_pr_review_total, decision=str(status)
        )


# ── test helpers ─────────────────────────────────────────────────────────

def _try_emit_log(span_ctx: Any, event_name: str, attrs: Dict[str, Any]) -> None:
    """Best-effort log emit — separate from span pipeline, never raises."""
    try:
        if span_ctx is not None and span_ctx.is_valid:
            emit_log_record(
                event_name=event_name,
                trace_id=span_ctx.trace_id,
                span_id=span_ctx.span_id,
                trace_flags=span_ctx.trace_flags,
                attributes=attrs,
            )
    except Exception:  # noqa: BLE001
        pass


def reset_dispatch_export() -> None:
    """Reseta estado mutable — apenas para testes."""
    global _drop_counter, _last_drop_log_ts, _sdk_warned
    with _spans_lock:
        for span in _active_spans.values():
            try:
                span.end()
            except Exception:  # noqa: BLE001
                pass
        _active_spans.clear()
    with _drop_lock:
        _drop_counter = 0
        _last_drop_log_ts = _time_fn()
    with _sdk_warned_lock:
        _sdk_warned = False
