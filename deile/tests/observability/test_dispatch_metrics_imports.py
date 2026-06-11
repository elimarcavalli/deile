"""AC12 + D6 — os.environ restrito + imports resolvem sem editar pyproject — #455.

AC12: exatamente 1 ocorrência de ``os.environ`` em ``dispatch_metrics.py``,
em ``_make_reader`` (``OTEL_METRIC_EXPORT_INTERVAL``); zero para ``DEILE_OTLP_*``
ou ``DEILE_OBSERVABILITY_DISABLED`` (lidos via ``get_observability_config()``).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_MODULE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "deile" / "observability" / "dispatch_metrics.py"
)


def test_os_environ_used_exactly_once():
    source = _MODULE.read_text(encoding="utf-8")
    matches = re.findall(r"os\.environ", source)
    assert len(matches) == 1, (
        f"esperado 1 uso de os.environ, achou {len(matches)}"
    )
    assert "OTEL_METRIC_EXPORT_INTERVAL" in source


def test_no_direct_deile_otlp_env_reads():
    source = _MODULE.read_text(encoding="utf-8")
    # Não deve LER DEILE_OTLP_* nem DEILE_OBSERVABILITY_DISABLED via os.environ
    # (menções em docstring são permitidas; o que importa é nenhum os.environ
    #  apontar para essas vars — elas vêm de get_observability_config()).
    assert not re.search(r'os\.environ[^\n]*DEILE_OTLP', source)
    assert not re.search(r'os\.environ[^\n]*DEILE_OBSERVABILITY', source)


def test_sdk_imports_resolve():
    """D6: imports do SDK de métricas resolvem (skip se SDK ausente)."""
    try:
        from opentelemetry.sdk.metrics import MeterProvider  # noqa: F401
    except ImportError:
        pytest.skip("opentelemetry SDK não instalado")
    # API de métricas e reader periódico disponíveis.
    from opentelemetry.sdk.metrics.export import \
        PeriodicExportingMetricReader  # noqa: F401


def test_module_exports():
    from deile.observability import dispatch_metrics as dm
    for name in (
        "record_dispatch_total",
        "record_dispatch_failed_total",
        "record_dispatch_duration_ms",
        "record_dispatch_tool_burst_total",
        "record_git_push_total",
        "record_forge_pr_review_total",
        "shutdown_dispatch_metrics",
        "reset_dispatch_metrics",
    ):
        assert hasattr(dm, name), f"missing export: {name}"
