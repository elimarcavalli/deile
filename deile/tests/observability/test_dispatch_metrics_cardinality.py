"""AC3 — labels de alta cardinalidade / segredo ausentes — issue #455.

Emite 1 ponto de cada métrica e itera todos os data points, confirmando que
nenhuma label proibida (``task_id``/``session_id``/``branch``/``sha``/``pr``/
``model``/``error_code``) aparece nas métricas de dispatch.
"""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm

pytestmark = pytest.mark.unit

_FORBIDDEN = {"task_id", "session_id", "branch", "sha", "pr", "model",
              "error_code"}


def test_no_forbidden_labels_anywhere(in_memory_dispatch_metrics_reader):
    dm.record_dispatch_total(role="worker", outcome="completed")
    dm.record_dispatch_failed_total(role="worker", reason="timeout")
    dm.record_dispatch_duration_ms(role="worker", outcome="failed", value_ms=12)
    dm.record_dispatch_tool_burst_total(role="worker", bucket="500+")
    dm.record_git_push_total(outcome="fail")
    dm.record_forge_pr_review_total(decision="CHANGES_REQUESTED")

    data = in_memory_dispatch_metrics_reader.get_metrics_data()
    seen_metrics = set()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                seen_metrics.add(metric.name)
                for dp in metric.data.data_points:
                    keys = set(dp.attributes.keys())
                    overlap = keys & _FORBIDDEN
                    assert not overlap, (
                        f"{metric.name} carrega label proibida: {overlap}"
                    )
    # 6 métricas públicas emitidas (otlp_drop é interno).
    assert dm.METRIC_DISPATCH_TOTAL in seen_metrics
    assert dm.METRIC_GIT_PUSH_TOTAL in seen_metrics


def test_allowlist_keys_disjoint_from_forbidden():
    """Nenhum set de allowlist contém uma label proibida."""
    for metric_name, allowed in dm._ALLOWED_LABELS.items():
        overlap = set(allowed) & _FORBIDDEN
        assert not overlap, f"{metric_name} allowlist inclui proibida: {overlap}"
