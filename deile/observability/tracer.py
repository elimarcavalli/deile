"""DeileTracer — wrapper sobre ``opentelemetry.trace`` com fallback no-op.

Quando OTLP está habilitado (``DEILE_OTLP_ENDPOINT`` set + SDK instalado),
:func:`get_tracer` retorna :class:`OtlpTracer`; caso contrário,
:class:`NoOpTracer` (de :mod:`deile.observability.no_op`).

Spans emitidos (schema do CNCF + atributos DEILE):

- ``deile.turn`` — 1 por interação usuário→agente (pai).
- ``deile.tool.<name>`` — 1 por execução de tool (filho do turn).
- ``deile.llm.call`` — 1 por chamada a provider LLM (filho do turn).

Regra crítica (princípio 11 + 08): nenhum atributo de span carrega
prompt/args/response/tokens em conteúdo. Apenas tamanhos (int) e identificadores
opacos (UUIDs, model handles, tool names).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from deile.observability.config import (ObservabilityConfig,
                                        get_observability_config)
from deile.observability.no_op import NoOpSpan, NoOpTracer

logger = logging.getLogger(__name__)

__all__ = [
    "DeileTracer",
    "OtlpTracer",
    "NoOpTracer",
    "get_tracer",
    "reset_tracer",
    "otel_available",
    "activate_traceparent_from_env",
]


def otel_available() -> bool:
    """Retorna ``True`` se o SDK OpenTelemetry está importável."""
    try:
        import opentelemetry  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel
        import opentelemetry.sdk.trace  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel
        return True
    except ImportError:
        return False


# Type alias: o real tracer ou seu fallback. Não dá pra usar Union[OtlpTracer, NoOpTracer]
# no escopo de módulo sem importar opentelemetry — o nome ``DeileTracer`` cobre ambos.
DeileTracer = Any  # type: ignore[assignment]


class OtlpTracer:
    """Wrapper sobre o ``opentelemetry.trace.Tracer`` do SDK.

    Singleton thread-safe (gerenciado em :func:`get_tracer`). Toda chamada de
    contexto (``turn`` / ``tool`` / ``llm_call``) é encapsulada em try/except
    que cai para ``NoOpSpan`` em caso de falha — observability nunca quebra o
    turn (princípio 11 + decisão da Fase 1).

    O setup do ``TracerProvider`` é lazy: ocorre na primeira chamada a
    :meth:`_ensure_provider`. Isso evita custo de I/O quando o tracer nunca
    é exercitado (ex: testes que não tocam o code path observado).
    """

    def __init__(self, config: Optional[ObservabilityConfig] = None) -> None:
        self._config = config or get_observability_config()
        self._provider: Any = None
        self._tracer: Any = None
        self._setup_lock = threading.Lock()
        self._shutdown = False

    # ── lazy setup ────────────────────────────────────────────────────────

    def _ensure_provider(self) -> Any:
        """Inicializa o ``TracerProvider`` no primeiro acesso (thread-safe)."""
        if self._tracer is not None:
            return self._tracer
        with self._setup_lock:
            if self._tracer is not None:
                return self._tracer
            try:
                self._build_provider()
            except Exception as exc:  # noqa: BLE001 — fail open
                logger.warning(
                    "OtlpTracer setup failed (%s); falling back to no-op", exc
                )
                self._tracer = None
                return None
            return self._tracer

    def _build_provider(self) -> None:
        """Configura o ``TracerProvider`` apontando para o collector OTLP."""
        from opentelemetry.sdk.resources import (  # pylint: disable=import-outside-toplevel
            SERVICE_NAME, Resource)
        from opentelemetry.sdk.trace import \
            TracerProvider  # pylint: disable=import-outside-toplevel
        from opentelemetry.sdk.trace.export import \
            BatchSpanProcessor  # pylint: disable=import-outside-toplevel
        from opentelemetry.sdk.trace.sampling import (  # pylint: disable=import-outside-toplevel
            ParentBased, TraceIdRatioBased)

        # Permite que testes injetem um provider já pronto via monkeypatch
        # do atributo de módulo ``_provider``. Quando isso acontece, usamos
        # o provider injetado em vez de criar um novo.
        injected = _module_injected_provider()
        if injected is not None:
            self._provider = injected
            self._tracer = injected.get_tracer("deile")
            return

        resource = Resource.create({SERVICE_NAME: self._config.service_name})
        sampler = ParentBased(
            root=TraceIdRatioBased(self._config.sample_ratio)
        )
        provider = TracerProvider(resource=resource, sampler=sampler)

        exporter = self._make_exporter()
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))

        self._provider = provider
        # NÃO chama trace.set_tracer_provider() — manter o provider isolado
        # para não interferir com outras libs que também configurem OTel.
        self._tracer = provider.get_tracer("deile")

    def _make_exporter(self) -> Any:
        """Constrói o ``OTLPSpanExporter`` (gRPC). Falha silenciosa → ``None``."""
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import \
                OTLPSpanExporter  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc não disponível "
                "(%s); spans serão acumulados em memória e perdidos.",
                exc,
            )
            return None
        try:
            return OTLPSpanExporter(
                endpoint=self._config.endpoint,
                insecure=self._config.insecure,
                headers=self._config.headers or None,
            )
        except Exception as exc:  # noqa: BLE001 — exporter init pode falhar
            logger.warning("OTLPSpanExporter init failed: %s", exc)
            return None

    # ── span helpers ──────────────────────────────────────────────────────

    @contextmanager
    def turn(
        self,
        session_id: str,
        turn_number: int,
        persona: str = "",
        model: str = "",
        input_length: int = 0,
    ) -> Iterator[Any]:
        """Span pai de uma interação usuário→agente."""
        attributes = {
            "deile.session.id": str(session_id or ""),
            "deile.turn.number": int(turn_number),
            "deile.input.length": int(input_length),
        }
        if persona:
            attributes["deile.persona"] = str(persona)
        if model:
            attributes["deile.model"] = str(model)
        # Resource attributes do InstanceState (issue #303 fase 1) — best-effort.
        _maybe_attach_instance_attrs(attributes)
        with self._start_span("deile.turn", attributes) as span:
            yield span

    @contextmanager
    def tool(
        self,
        tool_name: str,
        args_size: int = 0,
    ) -> Iterator[Any]:
        """Span filho — execução de uma tool."""
        attributes = {
            "deile.tool.name": str(tool_name),
            "deile.tool.args.size": int(args_size),
        }
        with self._start_span(f"deile.tool.{tool_name}", attributes) as span:
            yield span

    @contextmanager
    def llm_call(
        self,
        provider: str,
        model: str,
    ) -> Iterator[Any]:
        """Span filho — chamada a provider LLM. Caller seta tokens/cost no fim."""
        attributes = {
            "llm.provider": str(provider),
            "llm.model": str(model),
        }
        with self._start_span("deile.llm.call", attributes) as span:
            yield span

    @contextmanager
    def _start_span(
        self,
        name: str,
        attributes: Optional[dict] = None,
    ) -> Iterator[Any]:
        """Cria span do tracer real ou yield ``NoOpSpan`` se algo falhar."""
        if self._shutdown:
            yield NoOpSpan()
            return
        tracer = self._ensure_provider()
        if tracer is None:
            yield NoOpSpan()
            return
        try:
            span_cm = tracer.start_as_current_span(
                name=name,
                attributes=dict(attributes or {}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("start_as_current_span failed for %s: %s", name, exc)
            yield NoOpSpan()
            return
        # Exceção do bloco do usuário sobe normalmente — o SDK já marca o span
        # via ``record_exception`` no ``__exit__``.
        with span_cm as span:
            yield span

    # ── ciclo de vida ─────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Flush + shutdown do provider OTLP. Idempotente."""
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
            logger.debug("TracerProvider shutdown failed: %s", exc)


# ── attribute helpers ─────────────────────────────────────────────────────


def _maybe_attach_instance_attrs(attributes: dict) -> None:
    """Adiciona ``deile.instance.id`` / ``deile.instance.role`` se disponíveis.

    InstanceState (issue #303 fase 1) já carrega a identidade do processo —
    expondo isso como resource attribute permite correlacionar traces ao pod
    em backends como Tempo/Jaeger.

    Best-effort: tracer não pode quebrar se InstanceState não estiver setup.
    """
    try:
        from deile.runtime.instance_state import \
            get_instance_state  # pylint: disable=import-outside-toplevel
        state = get_instance_state()
        attributes["deile.instance.id"] = state.instance_id
        attributes["deile.instance.role"] = state.role
    except Exception:  # noqa: BLE001 — tracer nunca pode quebrar
        pass


def _module_injected_provider() -> Any:
    """Retorna ``_provider`` do módulo (monkeypatch por testes), ou ``None``.

    Suporta o pattern do test_tracer.py::

        monkeypatch.setattr("deile.observability.tracer._provider", custom_provider)
    """
    return globals().get("_provider", None)


# Marcador para monkeypatch em testes — fica ``None`` em produção, e a
# leitura via :func:`_module_injected_provider` cuida do override.
_provider: Any = None


def activate_traceparent_from_env() -> Any:
    """Extract W3C trace context from TRACEPARENT/TRACESTATE env vars and attach it.

    Called once at subprocess startup so all subsequent spans are nested under
    the parent span injected by OneshotSubprocessAgentBridge.

    Returns the attached context token (for detach), or None if not applicable.
    Best-effort: never raises.
    """
    traceparent = os.environ.get("TRACEPARENT") or os.environ.get("traceparent")
    if not traceparent:
        return None
    try:
        from opentelemetry.propagators.textmap import TraceContextTextMapPropagator  # pylint: disable=import-outside-toplevel
        carrier = {"traceparent": traceparent}
        tracestate = os.environ.get("TRACESTATE") or os.environ.get("tracestate")
        if tracestate:
            carrier["tracestate"] = tracestate
        ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
        import opentelemetry.context as otel_context  # pylint: disable=import-outside-toplevel
        token = otel_context.attach(ctx)
        return token
    except Exception as exc:  # noqa: BLE001
        logger.debug("activate_traceparent_from_env failed: %s", exc)
        return None


# ── singleton ────────────────────────────────────────────────────────────

_tracer_singleton: Optional[Any] = None
_singleton_lock = threading.Lock()


def get_tracer() -> Any:
    """Retorna o tracer singleton (OTLP real ou no-op).

    Decide com base em :func:`ObservabilityConfig.is_enabled` + disponibilidade
    do SDK. A primeira chamada cria; chamadas seguintes reutilizam.
    """
    global _tracer_singleton
    with _singleton_lock:
        if _tracer_singleton is None:
            config = get_observability_config()
            if config.is_enabled and otel_available():
                _tracer_singleton = OtlpTracer(config=config)
            else:
                _tracer_singleton = NoOpTracer()
        return _tracer_singleton


def reset_tracer() -> None:
    """Reseta o singleton — apenas para testes."""
    global _tracer_singleton
    with _singleton_lock:
        if _tracer_singleton is not None:
            try:
                _tracer_singleton.shutdown()
            except Exception:  # noqa: BLE001 — best-effort
                pass
            _tracer_singleton = None
