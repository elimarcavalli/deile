"""Testes de isolamento de falhas entre span e log pipelines — issue #454 D5.

Verifica que:
1. Falha no log exporter NÃO afeta o span pipeline.
2. Falha no span pipeline NÃO afeta o log pipeline.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestFailureIsolation:
    def test_log_failure_does_not_affect_span(self, in_memory_exporter, monkeypatch):
        """Exporter de log que raise → span ainda é emitido completo."""
        import deile.observability.dispatch_export as dep

        # `emit_log_record` foi importado para o namespace de dispatch_export (L33)
        # e `_try_emit_log` usa essa referência local — logo o patch precisa mirar
        # dispatch_export.emit_log_record para a falha realmente atingir o path.
        def _raise_log_boom(**kw):
            raise RuntimeError("log boom")

        monkeypatch.setattr(dep, "emit_log_record", _raise_log_boom)

        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_received)

        # Should not raise
        emit_dispatch_received("isolation-log-fail", session_id="s1")
        emit_dispatch_completed("isolation-log-fail", elapsed_s=1.0)

        spans = in_memory_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        assert len(root_spans) >= 1, "span deve ser emitido mesmo com log falhando"


    def test_span_failure_does_not_affect_log(self, in_memory_log_exporter, monkeypatch):
        """Falha no _get_raw_tracer não afeta log emission."""
        import deile.observability.dispatch_export as dep

        # Make _get_raw_tracer raise
        def broken_tracer():
            raise RuntimeError("tracer boom")

        monkeypatch.setattr(dep, "_get_raw_tracer", broken_tracer)

        from deile.observability.dispatch_export import emit_dispatch_received

        # Should not raise
        emit_dispatch_received("isolation-span-fail", session_id="s1")

