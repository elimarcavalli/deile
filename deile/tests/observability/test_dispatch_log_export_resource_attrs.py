"""Testes de resource attributes do LoggerProvider — issue #454 D1.

Verifica que os resource attrs do LoggerProvider são idênticos aos do
TracerProvider: service.name, deile.role, deile.pod, deile.dispatch.schema_version.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _otel_logs_available() -> bool:
    try:
        import opentelemetry.sdk._logs  # noqa: F401

        return True
    except ImportError:
        return False


class TestResourceAttributes:
    @pytest.mark.skipif(
        not _otel_logs_available(), reason="OTel logs SDK not available"
    )
    def test_service_name_in_resource(self, monkeypatch):
        """service.name resource attr configurável via DEILE_OTLP_SERVICE_NAME."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("DEILE_OTLP_SERVICE_NAME", "deile-custom")
        from deile.observability import (
            reset_dispatch_log_export,
            reset_observability_config,
        )

        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.config import get_observability_config
        from deile.observability.dispatch_log_export import _build_log_provider

        config = get_observability_config()
        provider = _build_log_provider(config)

        attrs = provider.resource.attributes
        assert attrs.get("service.name") == "deile-custom"

    @pytest.mark.skipif(
        not _otel_logs_available(), reason="OTel logs SDK not available"
    )
    def test_role_and_pod_in_resource(self, monkeypatch):
        """deile.role e deile.pod resource attrs vindos de DEILE_ROLE e HOSTNAME."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("DEILE_ROLE", "dispatch-worker")
        monkeypatch.setenv("HOSTNAME", "pod-xyz999")
        from deile.observability import (
            reset_dispatch_log_export,
            reset_observability_config,
        )

        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.config import get_observability_config
        from deile.observability.dispatch_log_export import _build_log_provider

        config = get_observability_config()
        provider = _build_log_provider(config)

        attrs = provider.resource.attributes
        assert attrs.get("deile.role") == "dispatch-worker"
        assert attrs.get("deile.pod") == "pod-xyz999"

    @pytest.mark.skipif(
        not _otel_logs_available(), reason="OTel logs SDK not available"
    )
    def test_schema_version_in_resource(self, monkeypatch):
        """deile.dispatch.schema_version deve ser SCHEMA_VERSION="1.0.0"."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        from deile.observability import (
            reset_dispatch_log_export,
            reset_observability_config,
        )

        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.config import get_observability_config
        from deile.observability.dispatch_log_export import _build_log_provider
        from deile.observability.dispatch_schema import SCHEMA_VERSION

        config = get_observability_config()
        provider = _build_log_provider(config)

        attrs = provider.resource.attributes
        assert attrs.get("deile.dispatch.schema_version") == SCHEMA_VERSION
        assert SCHEMA_VERSION == "1.0.0"

    @pytest.mark.skipif(
        not _otel_logs_available(), reason="OTel logs SDK not available"
    )
    def test_resource_attrs_match_tracer_provider(self, monkeypatch):
        """Resource attrs do LoggerProvider são idênticos ao TracerProvider (D1)."""
        monkeypatch.setenv("DEILE_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("DEILE_OTLP_SERVICE_NAME", "deile-parity")
        monkeypatch.setenv("DEILE_ROLE", "worker")
        monkeypatch.setenv("HOSTNAME", "pod-parity")
        from deile.observability import (
            reset_dispatch_log_export,
            reset_observability_config,
        )

        reset_observability_config()
        reset_dispatch_log_export()

        from deile.observability.config import get_observability_config
        from deile.observability.dispatch_log_export import _build_log_provider
        from deile.observability.dispatch_schema import (
            ATTR_POD,
            ATTR_ROLE,
            ATTR_SCHEMA_VERSION,
            SCHEMA_VERSION,
        )

        config = get_observability_config()
        provider = _build_log_provider(config)
        attrs = provider.resource.attributes

        # All 4 required resource attrs present
        assert "service.name" in attrs
        assert ATTR_ROLE in attrs
        assert ATTR_POD in attrs
        assert ATTR_SCHEMA_VERSION in attrs

        # Values match expectations
        assert attrs["service.name"] == "deile-parity"
        assert attrs[ATTR_ROLE] == "worker"
        assert attrs[ATTR_POD] == "pod-parity"
        assert attrs[ATTR_SCHEMA_VERSION] == SCHEMA_VERSION
