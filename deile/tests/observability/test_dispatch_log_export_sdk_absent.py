"""Testes de graceful degradation quando SDK ausente — issue #454 D6.

Verifica que:
- Linha INFO emitida na primeira chamada (não no module import).
- Linha INFO emitida apenas UMA vez.
- emit_log_record é no-op silencioso.
- Nenhuma exceção é propagada.
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit


class TestSdkAbsent:
    def test_no_op_when_logs_sdk_absent(self, monkeypatch):
        """SDK ausente → emit_log_record é no-op (não lança)."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        import deile.observability.dispatch_log_export as dle
        monkeypatch.setattr(dle, "otel_logs_available", lambda: False)

        # Should not raise
        dle.emit_log_record("dispatch.received", 1, 1, 1, {})

    def test_info_line_emitted_on_first_call(self, monkeypatch, caplog):
        """Linha INFO emitida na primeira chamada a get_log_provider() (D6 lazy)."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        import deile.observability.dispatch_log_export as dle
        monkeypatch.setattr(dle, "otel_logs_available", lambda: False)

        with caplog.at_level(logging.INFO, logger=dle._logger.name):
            dle.get_log_provider()

        assert "otel_sdk_available=false" in caplog.text
        assert "sink=disabled" in caplog.text

    def test_info_line_emitted_only_once(self, monkeypatch, caplog):
        """Linha INFO emitida apenas uma vez, não em chamadas subsequentes (D6)."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        import deile.observability.dispatch_log_export as dle
        monkeypatch.setattr(dle, "otel_logs_available", lambda: False)

        with caplog.at_level(logging.INFO, logger=dle._logger.name):
            dle.get_log_provider()
            dle.get_log_provider()
            dle.get_log_provider()
            dle.emit_log_record("dispatch.received", 1, 1, 1, {})
            dle.emit_log_record("dispatch.completed", 1, 1, 1, {})

        count = caplog.text.count("otel_sdk_available=false")
        assert count == 1, f"expected exactly 1 warning, got {count}"

    def test_info_line_not_emitted_at_module_import(self, monkeypatch, caplog):
        """Linha INFO NÃO é emitida no module import — apenas na primeira chamada (D6)."""
        import deile.observability.dispatch_log_export as dle

        # After import, if we haven't called get_log_provider(), no log should have been emitted
        # We check by looking at _sdk_warned before any call
        assert dle._sdk_warned is False, (
            "_sdk_warned should be False at module import — "
            "warning should be lazy (first call to get_log_provider)"
        )

    def test_get_log_provider_returns_none_when_sdk_absent(self, monkeypatch):
        """get_log_provider() retorna None quando SDK ausente."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        from deile.observability import reset_dispatch_log_export, reset_observability_config
        reset_observability_config()
        reset_dispatch_log_export()

        import deile.observability.dispatch_log_export as dle
        monkeypatch.setattr(dle, "otel_logs_available", lambda: False)

        result = dle.get_log_provider()
        assert result is None
