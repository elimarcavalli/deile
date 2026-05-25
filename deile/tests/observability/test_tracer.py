"""Testes do DeileTracer (OtlpTracer + NoOpTracer + injeção).

Cobertura: schema dos spans, atributos no esperado, status OK/ERROR,
record_exception, ciclo de vida (singleton, shutdown), fallback no-op quando
endpoint vazio ou SDK ausente.
"""

from __future__ import annotations

import os

import pytest

from deile.observability import NoOpTracer, get_tracer, reset_tracer
from deile.tests.observability.conftest import otel_sdk_available

pytestmark = pytest.mark.unit


def test_get_tracer_returns_no_op_when_endpoint_empty():
    assert os.environ.get("DEILE_OTLP_ENDPOINT", "") == ""
    tracer = get_tracer()
    assert isinstance(tracer, NoOpTracer)


def test_get_tracer_returns_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://x:4317")
    monkeypatch.setenv("DEILE_OBSERVABILITY_DISABLED", "true")
    reset_tracer()
    from deile.observability import reset_observability_config
    reset_observability_config()
    tracer = get_tracer()
    assert isinstance(tracer, NoOpTracer)


def test_get_tracer_singleton_idempotent():
    assert get_tracer() is get_tracer()


def test_no_op_tracer_turn_does_not_raise():
    tracer = NoOpTracer()
    with tracer.turn(session_id="s", turn_number=1, persona="dev") as span:
        span.set_attribute("x", 1)
        span.add_event("e")
        span.record_exception(RuntimeError("boom"))


def test_no_op_tracer_tool_does_not_raise():
    tracer = NoOpTracer()
    with tracer.tool("read_file", args_size=42) as span:
        span.set_attribute("x", 1)


def test_no_op_tracer_llm_call_does_not_raise():
    tracer = NoOpTracer()
    with tracer.llm_call("anthropic", "claude-3-5") as span:
        span.set_attribute("llm.tokens.in", 100)


# ── OTLP / SDK-backed tests ─────────────────────────────────────────────


def test_turn_span_records_required_attributes(in_memory_exporter):
    from deile.observability import get_tracer
    with get_tracer().turn(
        session_id="s1",
        turn_number=7,
        persona="dev",
        model="anthropic:sonnet",
        input_length=123,
    ):
        pass
    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "deile.turn"
    assert span.attributes["deile.session.id"] == "s1"
    assert span.attributes["deile.turn.number"] == 7
    assert span.attributes["deile.persona"] == "dev"
    assert span.attributes["deile.model"] == "anthropic:sonnet"
    assert span.attributes["deile.input.length"] == 123


def test_tool_span_named_with_tool_prefix(in_memory_exporter):
    from deile.observability import get_tracer
    with get_tracer().tool("execute_bash", args_size=64):
        pass
    spans = in_memory_exporter.get_finished_spans()
    assert any(s.name == "deile.tool.execute_bash" for s in spans)
    span = [s for s in spans if s.name == "deile.tool.execute_bash"][0]
    assert span.attributes["deile.tool.name"] == "execute_bash"
    assert span.attributes["deile.tool.args.size"] == 64


def test_llm_call_span_carries_provider_and_model(in_memory_exporter):
    from deile.observability import get_tracer
    with get_tracer().llm_call(provider="openai", model="gpt-4o") as span:
        span.set_attribute("llm.tokens.in", 100)
        span.set_attribute("llm.tokens.out", 50)
    spans = in_memory_exporter.get_finished_spans()
    assert any(s.name == "deile.llm.call" for s in spans)
    span = [s for s in spans if s.name == "deile.llm.call"][0]
    assert span.attributes["llm.provider"] == "openai"
    assert span.attributes["llm.model"] == "gpt-4o"
    assert span.attributes["llm.tokens.in"] == 100
    assert span.attributes["llm.tokens.out"] == 50


def test_span_records_status_error_on_exception(in_memory_exporter):
    if not otel_sdk_available():
        pytest.skip("SDK not available")
    from opentelemetry.trace import StatusCode

    from deile.observability import get_tracer
    with pytest.raises(RuntimeError):
        with get_tracer().tool("broken_tool"):
            raise RuntimeError("boom")
    spans = in_memory_exporter.get_finished_spans()
    # SDK marca status como ERROR e grava a exception
    span = [s for s in spans if s.name == "deile.tool.broken_tool"][0]
    # Pelo menos status é ERROR
    assert span.status.status_code == StatusCode.ERROR


def test_no_secrets_in_turn_span_attributes(in_memory_exporter):
    """Regra crítica (pilar 08): nenhum prompt/args/conteúdo no span."""
    secret_prompt = "MEU SEGREDO super privado API_KEY=sk-abcdef123"
    from deile.observability import get_tracer
    with get_tracer().turn(
        session_id="s1",
        turn_number=1,
        input_length=len(secret_prompt),  # Apenas tamanho, NÃO o conteúdo.
    ):
        pass
    spans = in_memory_exporter.get_finished_spans()
    span = spans[0]
    # Verificar que nenhum atributo contém o segredo
    for key, val in span.attributes.items():
        assert "sk-abcdef" not in str(val), f"segredo vazou no attr {key}"
        assert "SEGREDO" not in str(val), f"segredo vazou no attr {key}"


def test_no_secrets_in_tool_span_attributes(in_memory_exporter):
    secret_args = '{"path": "/etc/passwd", "API_KEY": "sk-leaky"}'
    from deile.observability import get_tracer
    with get_tracer().tool("read_file", args_size=len(secret_args)):
        pass
    spans = in_memory_exporter.get_finished_spans()
    span = [s for s in spans if s.name.startswith("deile.tool.")][0]
    for key, val in span.attributes.items():
        assert "sk-leaky" not in str(val)
        assert "/etc/passwd" not in str(val)
    # Mas o args.size deve estar lá (int, safe)
    assert span.attributes["deile.tool.args.size"] == len(secret_args)


def test_otlp_tracer_shutdown_is_idempotent(in_memory_exporter):
    from deile.observability import get_tracer
    tracer = get_tracer()
    tracer.shutdown()
    tracer.shutdown()  # não pode levantar


def test_instance_attrs_attached_to_turn_span(in_memory_exporter, monkeypatch, tmp_path):
    """resource attributes do InstanceState (issue #303 fase 1) viram span attrs."""
    monkeypatch.setenv("DEILE_RUNTIME_DIR", str(tmp_path))
    from deile.runtime import instance_state as runtime_mod
    runtime_mod.reset_instance_state()
    try:
        runtime_mod.get_instance_state(role="cli")
        from deile.observability import get_tracer
        with get_tracer().turn(session_id="s1", turn_number=1):
            pass
        spans = in_memory_exporter.get_finished_spans()
        span = [s for s in spans if s.name == "deile.turn"][0]
        assert "deile.instance.id" in span.attributes
        assert span.attributes["deile.instance.role"] == "cli"
    finally:
        runtime_mod.reset_instance_state()
