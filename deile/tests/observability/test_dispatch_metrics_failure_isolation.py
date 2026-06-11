"""AC6 — falha de métrica não derruba dispatch nem o span — issue #455.

Um instrument cujo ``add``/``record`` sempre raise: o ``emit_*`` real ainda
fecha o span com status OK (via ``InMemorySpanExporter`` de #443) e nenhuma
exceção propaga para o caller.
"""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm

pytestmark = pytest.mark.unit


class _Boom:
    def add(self, *a, **k):
        raise RuntimeError("metric exporter boom")

    def record(self, *a, **k):
        raise RuntimeError("metric exporter boom")


def test_metric_failure_does_not_break_dispatch_span(
    in_memory_exporter, monkeypatch
):
    from deile.observability.dispatch_export import (emit_dispatch_completed,
                                                     emit_dispatch_received)

    # Força provider "ligado" e troca todos os instruments por bombas.
    monkeypatch.setattr(dm, "_provider_tried", True)
    monkeypatch.setattr(dm, "_meter_provider_singleton", object())
    monkeypatch.setattr(
        dm, "_instruments", {name: _Boom() for name in dm._ALLOWED_LABELS}
    )

    # Não levanta apesar das métricas estourarem.
    emit_dispatch_received("boom-task", session_id="s1")
    emit_dispatch_completed("boom-task", elapsed_s=1.0, outcome="ok")

    spans = in_memory_exporter.get_finished_spans()
    root_spans = [s for s in spans if s.name == "deile.dispatch"]
    assert len(root_spans) >= 1
    root = root_spans[0]
    # Span fechado com status OK apesar do estouro de métrica.
    from opentelemetry.trace import StatusCode
    assert root.status.status_code == StatusCode.OK


def test_record_helper_swallows_instrument_error(monkeypatch):
    """O ``_add``/``_record`` engole exceção do instrument (best-effort)."""
    monkeypatch.setattr(dm, "_provider_tried", True)
    monkeypatch.setattr(dm, "_meter_provider_singleton", object())
    monkeypatch.setattr(
        dm, "_instruments", {dm.METRIC_DISPATCH_TOTAL: _Boom()}
    )
    # Não levanta.
    dm.record_dispatch_total(role="worker", outcome="completed")
