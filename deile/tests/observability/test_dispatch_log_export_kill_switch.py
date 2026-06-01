"""Testes do kill-switch DEILE_OTLP_LOGS_DISABLED — issue #454 D2.

Verifica as 4 combinações de estado:
1. Kill-switch isolado (logs disabled, spans continue).
2. Kill-switch global (ambos disabled).
3. Endpoint vazio (ambos no-op).
4. Apenas logs disabled (spans continuam funcionando).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestKillSwitch:
    def test_logs_disabled_no_log_records_emitted(self, in_memory_log_exporter, monkeypatch):
        """DEILE_OTLP_LOGS_DISABLED=true → zero LogRecords."""
        monkeypatch.setenv("DEILE_OTLP_LOGS_DISABLED", "true")
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.dispatch_log_export import (
            emit_log_record, get_log_provider)

        assert get_log_provider() is None, "provider deve ser None com kill-switch"
        emit_log_record("dispatch.received", 1, 1, 1, {})

        # Exporter was injected before reset, so nothing went through
        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 0

    def test_logs_disabled_spans_still_emitted(self, in_memory_exporter, monkeypatch):
        """DEILE_OTLP_LOGS_DISABLED=true não afeta spans (failure isolation D5)."""
        monkeypatch.setenv("DEILE_OTLP_LOGS_DISABLED", "true")
        from deile.observability import reset_observability_config
        reset_observability_config()

        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_received)

        emit_dispatch_received("kill-span-test", session_id="s1")
        emit_dispatch_completed("kill-span-test", elapsed_s=1.0)

        spans = in_memory_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        assert len(root_spans) >= 1, "spans devem continuar funcionando"

    def test_global_disabled_no_logs(self, monkeypatch):
        """DEILE_OBSERVABILITY_DISABLED=true → get_log_provider() retorna None."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("DEILE_OBSERVABILITY_DISABLED", "true")
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.dispatch_log_export import get_log_provider

        assert get_log_provider() is None

    def test_empty_endpoint_no_logs(self, monkeypatch):
        """Endpoint vazio → get_log_provider() retorna None."""
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.dispatch_log_export import get_log_provider

        # No endpoint set (default state from conftest _reset_singletons)
        assert get_log_provider() is None

    def test_only_logs_disabled_config_field(self, monkeypatch):
        """ObservabilityConfig.logs_disabled parses DEILE_OTLP_LOGS_DISABLED."""
        monkeypatch.setenv("DEILE_OTLP_LOGS_DISABLED", "true")
        from deile.observability import reset_observability_config
        reset_observability_config()

        from deile.observability.config import get_observability_config
        config = get_observability_config()
        assert config.logs_disabled is True

    def test_logs_disabled_default_false(self, monkeypatch):
        """Por padrão, logs_disabled é False."""
        from deile.observability.config import get_observability_config
        config = get_observability_config()
        assert config.logs_disabled is False
