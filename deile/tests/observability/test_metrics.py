"""Testes do DeileMetrics (OtlpMetrics + NoOpMetrics)."""

from __future__ import annotations

import pytest

from deile.observability import NoOpMetrics, get_metrics

pytestmark = pytest.mark.unit


def test_get_metrics_returns_no_op_when_endpoint_empty():
    m = get_metrics()
    assert isinstance(m, NoOpMetrics)


def test_get_metrics_singleton_idempotent():
    assert get_metrics() is get_metrics()


def test_no_op_record_methods_do_not_raise():
    m = NoOpMetrics()
    m.record_tokens("anthropic", "claude-3-5", "in", 100)
    m.record_tokens("openai", "gpt-4o", "out", 50)
    m.record_cost("anthropic", "claude-3-5", 0.05)
    m.record_tool_duration("read_file", "success", 23)
    m.record_turn_duration("dev", 1500)
    m.record_error("TimeoutError", "tool_loop")


def test_no_op_metrics_shutdown_idempotent():
    m = NoOpMetrics()
    m.shutdown()
    m.shutdown()


def test_no_op_record_tokens_zero_count_is_safe():
    m = NoOpMetrics()
    m.record_tokens("anthropic", "claude-3-5", "in", 0)


# ── SDK-backed tests ────────────────────────────────────────────────────


def _collect_metric_data_points(reader):
    """Helper: extrai (name, [(value, attributes)]) pares do reader."""
    data = reader.get_metrics_data()
    out = {}
    if not data:
        return out
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points = []
                for dp in metric.data.data_points:
                    val = (
                        getattr(dp, "value", None)
                        if getattr(dp, "value", None) is not None
                        else getattr(dp, "sum", None)
                    )
                    points.append((val, dict(dp.attributes)))
                out[metric.name] = points
    return out


def test_record_tokens_emits_counter(in_memory_metrics_reader):
    m = get_metrics()
    m.record_tokens("anthropic", "claude-3-5", "in", 100)
    m.record_tokens("anthropic", "claude-3-5", "out", 50)
    points = _collect_metric_data_points(in_memory_metrics_reader)
    assert "deile.tokens.total" in points
    by_dir = {p[1]["direction"]: p[0] for p in points["deile.tokens.total"]}
    assert by_dir["in"] == 100
    assert by_dir["out"] == 50


def test_record_cost_emits_counter(in_memory_metrics_reader):
    m = get_metrics()
    m.record_cost("openai", "gpt-4o", 0.12)
    m.record_cost("openai", "gpt-4o", 0.08)
    points = _collect_metric_data_points(in_memory_metrics_reader)
    assert "deile.cost.usd.total" in points
    total = sum(p[0] for p in points["deile.cost.usd.total"])
    assert abs(total - 0.20) < 1e-6


def test_record_tool_duration_emits_histogram(in_memory_metrics_reader):
    m = get_metrics()
    m.record_tool_duration("read_file", "success", 23)
    m.record_tool_duration("read_file", "success", 47)
    m.record_tool_duration("write_file", "error", 100)
    points = _collect_metric_data_points(in_memory_metrics_reader)
    assert "deile.tool.duration_ms" in points
    # histogram sum por (tool_name, status) — basta confirmar a presença
    found_pairs = {(p[1]["tool_name"], p[1]["status"]) for p in points["deile.tool.duration_ms"]}
    assert ("read_file", "success") in found_pairs
    assert ("write_file", "error") in found_pairs


def test_record_turn_duration_emits_histogram(in_memory_metrics_reader):
    m = get_metrics()
    m.record_turn_duration("dev", 1500)
    points = _collect_metric_data_points(in_memory_metrics_reader)
    assert "deile.turn.duration_ms" in points


def test_record_error_emits_counter_with_labels(in_memory_metrics_reader):
    m = get_metrics()
    m.record_error("TimeoutError", "tool_loop")
    m.record_error("ValueError", "agent")
    points = _collect_metric_data_points(in_memory_metrics_reader)
    assert "deile.errors.total" in points
    pairs = {(p[1]["error_type"], p[1]["component"]) for p in points["deile.errors.total"]}
    assert ("TimeoutError", "tool_loop") in pairs
    assert ("ValueError", "agent") in pairs


def test_metrics_no_session_id_label(in_memory_metrics_reader):
    """Cardinality controlada: session_id NÃO deve aparecer como label."""
    m = get_metrics()
    m.record_tokens("anthropic", "claude-3-5", "in", 100)
    m.record_cost("anthropic", "claude-3-5", 0.05)
    m.record_error("TimeoutError", "tool_loop")
    points = _collect_metric_data_points(in_memory_metrics_reader)
    for metric_name, dps in points.items():
        for _val, attrs in dps:
            assert "session_id" not in attrs, (
                f"session_id leaked in metric {metric_name} attrs: {attrs}"
            )


def test_metrics_shutdown_is_idempotent(in_memory_metrics_reader):
    m = get_metrics()
    m.record_tokens("anthropic", "claude-3-5", "in", 100)
    m.shutdown()
    m.shutdown()  # idempotente, não levanta
