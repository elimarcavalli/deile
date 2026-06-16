"""AC11 — fiação REAL ``emit_* → record_*`` — issue #455.

Resolve a objeção do gate (2b2 / GC #596): NÃO mocka ``DispatchExport.emit_*``.
Dirige os ``emit_*`` REAIS de ``dispatch_export`` (de #443) e asserta que os
counters/histograms de dispatch foram incrementados via ``InMemoryMetricReader``.
Se a fiação for removida, estes testes FALHAM — ao contrário de um mock do
call-site, que provaria apenas que ``record_*`` funciona quando chamado.
"""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm
from deile.tests.observability.conftest import dispatch_metric_points

pytestmark = pytest.mark.unit


def _point_with(points, attrs):
    return [(v, a) for v, a in points if a == attrs]


class TestRealWiring:
    def test_completed_increments_total_and_duration(
        self, in_memory_dispatch_metrics_reader, monkeypatch
    ):
        """emit_dispatch_completed REAL → total{completed}+=1 + duration_ms."""
        monkeypatch.setenv("DEILE_ROLE", "worker")
        from deile.observability.config import reset_observability_config

        reset_observability_config()

        from deile.observability.dispatch_export import (
            emit_dispatch_completed,
            emit_dispatch_received,
        )

        emit_dispatch_received("T1", session_id="s1")
        # elapsed_s=5.432 → 5432 ms.
        emit_dispatch_completed("T1", elapsed_s=5.432, outcome="ok")

        total = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_TOTAL
        )
        completed = _point_with(total, {"role": "worker", "outcome": "completed"})
        assert completed and completed[0][0] == 1

        dur = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_DURATION_MS
        )
        dur_pt = _point_with(dur, {"role": "worker", "outcome": "completed"})
        assert dur_pt and dur_pt[0][0] == 5432

    def test_failed_increments_total_failed_and_duration(
        self, in_memory_dispatch_metrics_reader, monkeypatch
    ):
        """emit_dispatch_failed REAL → total{failed} + failed{reason} + duration."""
        monkeypatch.setenv("DEILE_ROLE", "worker")
        from deile.observability.config import reset_observability_config

        reset_observability_config()

        from deile.observability.dispatch_export import (
            emit_dispatch_failed,
            emit_dispatch_received,
        )

        emit_dispatch_received("T2", session_id="s1")
        emit_dispatch_failed("T2", reason="auth_expired", elapsed_s=2.0)

        total = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_TOTAL
        )
        assert _point_with(total, {"role": "worker", "outcome": "failed"})

        failed = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_FAILED_TOTAL
        )
        assert _point_with(failed, {"role": "worker", "reason": "auth_expired"})

        dur = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_DURATION_MS
        )
        dur_pt = _point_with(dur, {"role": "worker", "outcome": "failed"})
        assert dur_pt and dur_pt[0][0] == 2000

    def test_tool_burst_real_wiring_buckets(
        self, in_memory_dispatch_metrics_reader, monkeypatch
    ):
        """emit_dispatch_tool_burst(count=65) REAL → bucket '100-'."""
        monkeypatch.setenv("DEILE_ROLE", "worker")
        from deile.observability.config import reset_observability_config

        reset_observability_config()

        from deile.observability.dispatch_export import (
            emit_dispatch_received,
            emit_dispatch_tool_burst,
        )

        emit_dispatch_received("T3", session_id="s1")
        emit_dispatch_tool_burst("T3", tools="read,write", count=65)

        burst = dispatch_metric_points(
            in_memory_dispatch_metrics_reader,
            dm.METRIC_DISPATCH_TOOL_BURST_TOTAL,
        )
        assert _point_with(burst, {"role": "worker", "bucket": "100-"})

    def test_git_push_real_wiring(self, in_memory_dispatch_metrics_reader, monkeypatch):
        """emit_git_push(status='ok') REAL → git.push.total{outcome=ok}."""
        monkeypatch.setenv("DEILE_ROLE", "worker")
        from deile.observability.config import reset_observability_config

        reset_observability_config()

        from deile.observability.dispatch_export import (
            emit_dispatch_received,
            emit_git_push,
        )

        emit_dispatch_received("T4", session_id="s1")
        emit_git_push("T4", repo="o/r", branch="b", status="ok")

        push = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_GIT_PUSH_TOTAL
        )
        assert _point_with(push, {"outcome": "ok"})

    def test_forge_pr_review_real_wiring(
        self, in_memory_dispatch_metrics_reader, monkeypatch
    ):
        """emit_forge_pr_review(status='APPROVED') REAL → pr_review{decision}."""
        monkeypatch.setenv("DEILE_ROLE", "worker")
        from deile.observability.config import reset_observability_config

        reset_observability_config()

        from deile.observability.dispatch_export import (
            emit_dispatch_received,
            emit_forge_pr_review,
        )

        emit_dispatch_received("T5", session_id="s1")
        emit_forge_pr_review("T5", repo="o/r", pr_number=42, status="APPROVED")

        review = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_FORGE_PR_REVIEW_TOTAL
        )
        assert _point_with(review, {"decision": "APPROVED"})

    def test_wiring_absent_would_be_caught(
        self, in_memory_dispatch_metrics_reader, monkeypatch
    ):
        """Prova negativa: sem a chamada real, a métrica fica zerada.

        Garante que o teste de fiação real não é trivialmente verde — se o hook
        não fosse exercitado, este caminho não produziria pontos.
        """
        monkeypatch.setenv("DEILE_ROLE", "worker")
        from deile.observability.config import reset_observability_config

        reset_observability_config()

        # Nenhum emit chamado → zero pontos de dispatch.total.
        points = dispatch_metric_points(
            in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_TOTAL
        )
        assert points == []
