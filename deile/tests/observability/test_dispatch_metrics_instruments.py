"""AC1 (emissão) + AC2 (allowlist) — issue #455.

Cada uma das 7 métricas: emite 1 data point correto (AC1) e rejeita label fora
do set fechado (AC2). Exercita os ``record_*`` reais via ``InMemoryMetricReader``.
"""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm
from deile.tests.observability.conftest import dispatch_metric_points

pytestmark = pytest.mark.unit


class TestEmission:
    def test_dispatch_total_one_point(self, in_memory_dispatch_metrics_reader):
        dm.record_dispatch_total(role="worker", outcome="completed")
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_TOTAL
        )
        assert len(points) >= 1
        value, attrs = points[0]
        assert value == 1
        assert attrs == {"role": "worker", "outcome": "completed"}

    def test_dispatch_failed_one_point(self, in_memory_dispatch_metrics_reader):
        dm.record_dispatch_failed_total(role="worker", reason="auth_expired")
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_FAILED_TOTAL
        )
        value, attrs = points[0]
        assert value == 1
        assert attrs == {"role": "worker", "reason": "auth_expired"}

    def test_dispatch_duration_one_point(self, in_memory_dispatch_metrics_reader):
        dm.record_dispatch_duration_ms(
            role="worker", outcome="completed", value_ms=5432
        )
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_DURATION_MS
        )
        value, attrs = points[0]
        assert value == 5432
        assert attrs == {"role": "worker", "outcome": "completed"}

    def test_tool_burst_one_point(self, in_memory_dispatch_metrics_reader):
        dm.record_dispatch_tool_burst_total(role="worker", bucket="100-")
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader,
            dm.METRIC_DISPATCH_TOOL_BURST_TOTAL,
        )
        value, attrs = points[0]
        assert value == 1
        assert attrs == {"role": "worker", "bucket": "100-"}

    def test_git_push_one_point(self, in_memory_dispatch_metrics_reader):
        dm.record_git_push_total(outcome="ok")
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_GIT_PUSH_TOTAL
        )
        value, attrs = points[0]
        assert value == 1
        assert attrs == {"outcome": "ok"}

    def test_forge_pr_review_one_point(self, in_memory_dispatch_metrics_reader):
        dm.record_forge_pr_review_total(decision="APPROVED")
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_FORGE_PR_REVIEW_TOTAL
        )
        value, attrs = points[0]
        assert value == 1
        assert attrs == {"decision": "APPROVED"}

    def test_otlp_drop_emitted_via_drop_loop(
        self, in_memory_dispatch_metrics_reader, monkeypatch
    ):
        """A 7ª métrica (otlp_drop) é emitida pelo drop counter no flush.

        Cobre o caminho real de drop: contador local → flush throttled →
        ``_emit_drop_metric`` → ``deile.dispatch.otlp_drop.total``.
        """
        # Força o init do provider (instruments prontos).
        assert dm._get_dispatch_meter_provider() is not None

        # Relógio mockado: 1º drop arma o contador; 2º (100s depois) flusha.
        ticks = iter([0.0, 100.0])
        monkeypatch.setattr(dm, "_time_fn", lambda: next(ticks))
        dm._record_drop("export_error")  # arma (count=1, now=0)
        dm._record_drop("export_error")  # now=100 → flush do período (count=1)

        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_OTLP_DROP_TOTAL
        )
        assert len(points) >= 1
        value, attrs = points[0]
        assert value >= 1
        assert attrs == {"reason": "export_error"}


_ALL_RECORDERS = [
    (
        dm.record_dispatch_total,
        {"role": "w", "outcome": "completed"},
        dm.METRIC_DISPATCH_TOTAL,
    ),
    (
        dm.record_dispatch_failed_total,
        {"role": "w", "reason": "timeout"},
        dm.METRIC_DISPATCH_FAILED_TOTAL,
    ),
    (
        dm.record_dispatch_duration_ms,
        {"role": "w", "outcome": "completed", "value_ms": 10},
        dm.METRIC_DISPATCH_DURATION_MS,
    ),
    (
        dm.record_dispatch_tool_burst_total,
        {"role": "w", "bucket": "50-"},
        dm.METRIC_DISPATCH_TOOL_BURST_TOTAL,
    ),
    (dm.record_git_push_total, {"outcome": "ok"}, dm.METRIC_GIT_PUSH_TOTAL),
    (
        dm.record_forge_pr_review_total,
        {"decision": "APPROVED"},
        dm.METRIC_FORGE_PR_REVIEW_TOTAL,
    ),
]


class TestAllowlist:
    @pytest.mark.parametrize("fn,kwargs,metric_name", _ALL_RECORDERS)
    def test_forbidden_label_raises(self, fn, kwargs, metric_name):
        """AC2: cada recorder rejeita uma label fora do set fechado."""
        with pytest.raises(ValueError) as exc:
            fn(task_id="X", **kwargs)
        assert "task_id" in str(exc.value)
        assert metric_name in str(exc.value)

    def test_otlp_drop_allowlist(self):
        """A métrica interna otlp_drop só aceita 'reason'."""
        with pytest.raises(ValueError):
            dm._validate_labels(dm.METRIC_DISPATCH_OTLP_DROP_TOTAL, {"role": "w"})
        # 'reason' é aceito.
        dm._validate_labels(dm.METRIC_DISPATCH_OTLP_DROP_TOTAL, {"reason": "x"})

    def test_seven_metrics_have_allowlist(self):
        """Há exatamente 7 métricas declaradas com allowlist (D2)."""
        assert len(dm._ALLOWED_LABELS) == 7
