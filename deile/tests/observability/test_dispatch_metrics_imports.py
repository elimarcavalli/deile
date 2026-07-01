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






def test_shutdown_dispatch_metrics_callable_and_idempotent():
    """``shutdown_dispatch_metrics`` é o único símbolo não exercitado
    comportamentalmente nos testes vizinhos — os demais exports
    (``record_*`` e ``reset_dispatch_metrics``) já são CHAMADOS diretamente em
    ``test_dispatch_metrics_instruments``/``_cardinality``/``conftest``, o que
    garante sua existência de forma mais forte que ``hasattr``. Aqui exercitamos
    o shutdown de fato: existe, é chamável, never-raises e é idempotente
    (cobre o early-return quando não há provider)."""
    from deile.observability import dispatch_metrics as dm

    assert callable(dm.shutdown_dispatch_metrics)
    dm.shutdown_dispatch_metrics()
    dm.shutdown_dispatch_metrics()
