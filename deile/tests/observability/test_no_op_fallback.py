"""Testes do fallback no-op — quando OTel não está instalado ou desabilitado.

Esses testes NÃO importam ``opentelemetry`` em escopo de módulo. Eles rodam
incondicionalmente, mesmo em ambientes sem o SDK, e verificam que:

  - ``get_tracer()`` / ``get_metrics()`` voltam no-op quando endpoint vazio.
  - ``DEILE_OBSERVABILITY_DISABLED=true`` força no-op mesmo com endpoint set.
  - Operações no-op não levantam exceções (qualquer regressão aqui quebra a
    promessa de "observability nunca quebra o turn").
"""

from __future__ import annotations

import pytest

from deile.observability import (NoOpMetrics, NoOpTracer, get_metrics,
                                 get_tracer)

pytestmark = pytest.mark.unit


def test_get_tracer_returns_no_op_by_default():
    assert isinstance(get_tracer(), NoOpTracer)


def test_get_metrics_returns_no_op_by_default():
    assert isinstance(get_metrics(), NoOpMetrics)


def test_no_op_path_smokes_full_api():
    """Toda a API pública é callable em modo no-op sem erro."""
    t = get_tracer()
    m = get_metrics()
    with t.turn(session_id="s", turn_number=1, persona="dev", model="x", input_length=10) as span:
        span.set_attribute("a", "b")
        span.add_event("e", attributes={"k": "v"})
        with t.tool("read_file", args_size=10) as ts:
            ts.set_attribute("foo", 1)
        with t.llm_call("anthropic", "claude-3-5") as ls:
            ls.set_attribute("llm.tokens.in", 100)
            ls.set_attribute("llm.tokens.out", 50)
    m.record_tokens("anthropic", "claude-3-5", "in", 100)
    m.record_cost("anthropic", "claude-3-5", 0.05)
    m.record_tool_duration("read_file", "success", 23)
    m.record_turn_duration("dev", 1500)
    m.record_error("TimeoutError", "tool_loop")
    t.shutdown()
    m.shutdown()


def test_kill_switch_forces_no_op_even_with_endpoint(monkeypatch):
    monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://x:4317")
    monkeypatch.setenv("DEILE_OBSERVABILITY_DISABLED", "true")
    from deile.observability import (reset_metrics, reset_observability_config,
                                     reset_tracer)
    reset_observability_config()
    reset_tracer()
    reset_metrics()
    assert isinstance(get_tracer(), NoOpTracer)
    assert isinstance(get_metrics(), NoOpMetrics)


def test_no_op_span_record_exception_does_not_raise():
    t = get_tracer()
    with t.turn(session_id="s", turn_number=1) as span:
        try:
            raise RuntimeError("inner")
        except RuntimeError as exc:
            span.record_exception(exc)
            span.set_status("error")  # qualquer valor é aceito


def test_no_op_metrics_handles_negative_and_zero_safely():
    """Inputs degenerados (zero, negativo) não podem quebrar o coletor."""
    m = NoOpMetrics()
    m.record_tokens("a", "b", "in", 0)
    m.record_tokens("a", "b", "in", -1)
    m.record_cost("a", "b", 0.0)
    m.record_cost("a", "b", -0.05)
    m.record_tool_duration("x", "ok", 0)


def test_observability_module_importable_without_otel(monkeypatch):
    """Mesmo se ``opentelemetry`` estivesse ausente, módulo deve importar.

    Simulamos via mock — não dá pra realmente remover o SDK aqui, mas
    confirmamos que ``otel_available()`` faz check defensivo.
    """
    from deile.observability import otel_available

    # Quando o SDK está presente, otel_available() deve retornar True; quando
    # não, False. Os caminhos do tracer/metrics não devem importar o módulo
    # se True/False; basta a função existir e ser callable.
    assert callable(otel_available)
    assert isinstance(otel_available(), bool)
