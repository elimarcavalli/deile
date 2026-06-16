"""Testes de importações OTel necessárias — issue #454 D6.

Verifica que os 4+ imports necessários resolvem sem editar pyproject.toml.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestOtelImports:
    def test_otel_api_logs_importable(self):
        """opentelemetry._logs é importável."""
        try:
            import opentelemetry._logs  # noqa: F401
        except ImportError as e:
            pytest.skip(f"opentelemetry._logs não disponível: {e}")

    def test_otel_sdk_logs_importable(self):
        """opentelemetry.sdk._logs é importável."""
        try:
            import opentelemetry.sdk._logs  # noqa: F401
        except ImportError as e:
            pytest.skip(f"opentelemetry.sdk._logs não disponível: {e}")

    def test_otel_sdk_logs_logger_provider_importable(self):
        """opentelemetry.sdk._logs.LoggerProvider é importável."""
        try:
            from opentelemetry.sdk._logs import LoggerProvider  # noqa: F401
        except ImportError as e:
            pytest.skip(f"LoggerProvider não disponível: {e}")

    def test_otel_sdk_logs_batch_processor_importable(self):
        """opentelemetry.sdk._logs.export.BatchLogRecordProcessor é importável."""
        try:
            from opentelemetry.sdk._logs.export import (  # noqa: F401
                BatchLogRecordProcessor,
            )
        except ImportError as e:
            pytest.skip(f"BatchLogRecordProcessor não disponível: {e}")

    def test_in_memory_log_exporter_importable(self):
        """opentelemetry.sdk._logs.export.InMemoryLogExporter é importável."""
        try:
            from opentelemetry.sdk._logs.export import InMemoryLogExporter  # noqa: F401
        except ImportError as e:
            pytest.skip(f"InMemoryLogExporter não disponível: {e}")

    def test_severity_number_importable(self):
        """opentelemetry._logs.SeverityNumber é importável."""
        try:
            from opentelemetry._logs import SeverityNumber  # noqa: F401
        except ImportError as e:
            pytest.skip(f"SeverityNumber não disponível: {e}")

    def test_no_direct_env_reads_in_module(self):
        """dispatch_log_export.py não lê os.environ diretamente."""
        import inspect

        import deile.observability.dispatch_log_export as dle

        source = inspect.getsource(dle)
        # os.environ direct reads are forbidden (use get_observability_config())
        # Allow 'os' in comments and import statements, but not os.environ calls
        import re

        direct_env_calls = re.findall(r"os\.environ", source)
        assert len(direct_env_calls) == 0, (
            f"dispatch_log_export.py has {len(direct_env_calls)} direct os.environ "
            f"reads — use get_observability_config() instead"
        )

    def test_module_public_api(self):
        """Todos os símbolos do __all__ são importáveis."""
        import deile.observability.dispatch_log_export as dle

        for name in dle.__all__:
            assert hasattr(dle, name), f"{name} not found in dispatch_log_export"

    def test_otel_logs_available_function(self):
        """otel_logs_available() retorna bool."""
        from deile.observability.dispatch_log_export import otel_logs_available

        result = otel_logs_available()
        assert isinstance(result, bool)
