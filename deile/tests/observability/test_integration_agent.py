"""Integração end-to-end: spans emitidos durante o tool loop e providers.

Exercita os pontos de integração reais (``_record_usage`` em providers,
``ToolLoopExecutor`` por tool, ``DeileAgent.process_input`` por turn) com
backend in-memory + SDK real, sem chamar nenhum LLM externo.

Foco: confirmar que QUANDO OTLP está ligado, os pontos certos do código
emitem spans com nomes e atributos esperados. Não testamos o conteúdo da
resposta da LLM — esses fluxos são cobertos pelos testes específicos de cada
camada.
"""

from __future__ import annotations

import pytest

from deile.observability import get_tracer
from deile.tests.observability.conftest import otel_sdk_available

pytestmark = pytest.mark.integration


async def test_record_usage_in_base_emits_token_metrics(in_memory_metrics_reader):
    """Toda chamada de ``_record_usage`` deve emitir tokens + cost."""
    from deile.core.models.base import ModelProvider, ModelUsage

    # Subclass mínima — ModelProvider é ABC; precisamos satisfazer abstract
    # methods, mas só vamos chamar ``_record_usage`` diretamente.
    class _FakeProvider(ModelProvider):
        provider_id = "fakeprov"
        tier = "balanced"
        model_size = type("S", (), {"value": "medium"})()
        supported_types = []

        @property
        def provider_name(self) -> str:
            return "fakeprov"

        async def generate(self, *a, **kw):
            raise NotImplementedError

        async def generate_stream(self, *a, **kw):
            raise NotImplementedError

    prov = _FakeProvider(model_name="fake-model")
    usage = ModelUsage(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cached_tokens=20,
        cost_estimate=0.12,
    )
    await prov._record_usage(
        session_id="s1",
        usage=usage,
        latency_ms=345,
        success=True,
    )

    data = in_memory_metrics_reader.get_metrics_data()
    metrics_by_name = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                metrics_by_name[m.name] = m

    assert "deile.tokens.total" in metrics_by_name
    assert "deile.cost.usd.total" in metrics_by_name
    # Confirmar que 100 + 50 + 20 viraram 3 contagens diferentes (in/out/cached).
    tok_pts = list(metrics_by_name["deile.tokens.total"].data.data_points)
    dirs = {dp.attributes["direction"]: dp.value for dp in tok_pts}
    assert dirs["in"] == 100
    assert dirs["out"] == 50
    assert dirs["cached"] == 20


async def test_tool_loop_executor_emits_tool_span_and_duration(
    in_memory_exporter, in_memory_metrics_reader
):
    """ToolLoopExecutor cria ``deile.tool.<name>`` + ``deile.tool.duration_ms``."""
    if not otel_sdk_available():
        pytest.skip("SDK not available")

    # Exercita os helpers internos diretamente (simulam o que a loop faz).
    import time

    from deile.core.tool_loop_executor import (
        _record_tool_metrics,
        _set_tool_span_status,
    )

    tracer = get_tracer()
    with tracer.tool("custom_tool", args_size=42) as span:
        _set_tool_span_status(span, is_success=True)
    _record_tool_metrics("custom_tool", "success", time.monotonic() - 0.05)

    spans = in_memory_exporter.get_finished_spans()
    matching = [s for s in spans if s.name == "deile.tool.custom_tool"]
    assert matching, f"expected deile.tool.custom_tool, got {[s.name for s in spans]}"
    span = matching[0]
    assert span.attributes["deile.tool.name"] == "custom_tool"
    assert span.attributes["deile.tool.args.size"] == 42
    assert span.attributes["deile.tool.result.status"] == "success"

    data = in_memory_metrics_reader.get_metrics_data()
    metric_names = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                metric_names.add(m.name)
    assert "deile.tool.duration_ms" in metric_names


async def test_tool_loop_executor_error_helper_sets_error_status(in_memory_exporter):
    if not otel_sdk_available():
        pytest.skip("SDK not available")
    from opentelemetry.trace import StatusCode

    from deile.core.tool_loop_executor import _set_tool_span_error

    tracer = get_tracer()
    with tracer.tool("broken_tool", args_size=0) as span:
        try:
            raise RuntimeError("simulated")
        except RuntimeError as exc:
            _set_tool_span_error(span, exc)

    spans = in_memory_exporter.get_finished_spans()
    span = [s for s in spans if s.name == "deile.tool.broken_tool"][0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["deile.tool.result.status"] == "error"
    # Evento ``deile.tool.error`` deve ter sido emitido
    event_names = [e.name for e in span.events]
    assert "deile.tool.error" in event_names


async def test_agent_helpers_record_turn_error_and_finalize(
    in_memory_exporter, in_memory_metrics_reader
):
    """``_record_turn_error`` + ``_finalize_turn_span`` cobrem o ciclo do turn."""
    if not otel_sdk_available():
        pytest.skip("SDK not available")
    from opentelemetry.trace import StatusCode

    from deile.core.agent import _finalize_turn_span, _record_turn_error

    tracer = get_tracer()
    cm = tracer.turn(session_id="s1", turn_number=1, persona="dev")
    span = cm.__enter__()
    try:
        raise ValueError("turn failed")
    except ValueError as exc:
        _record_turn_error(span, exc, component="process_input")
    finally:
        _finalize_turn_span(cm, duration_ms=2500, persona="dev")

    spans = in_memory_exporter.get_finished_spans()
    turn_span = [s for s in spans if s.name == "deile.turn"][0]
    assert turn_span.status.status_code == StatusCode.ERROR

    data = in_memory_metrics_reader.get_metrics_data()
    metric_names = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                metric_names.add(m.name)
    # Ambos deile.turn.duration_ms (do finalize) e deile.errors.total (do record).
    assert "deile.turn.duration_ms" in metric_names
    assert "deile.errors.total" in metric_names
