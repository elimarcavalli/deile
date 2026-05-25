"""Isolamento de runtime state para o subpacote de testes.

Cada teste roda com um ``DEILE_RUNTIME_DIR`` apontando para um diretório
temporário e com o singleton global de :mod:`deile.runtime.instance_state`
resetado — assim não há contaminação cruzada entre testes nem com o
``~/.deile/run/`` real do dev.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(tmp_path, monkeypatch):
    """Redireciona o runtime dir para ``tmp_path`` e reseta o singleton.

    Análogo ao ``_isolate_audit_logger`` no conftest raiz — escopo limitado
    a ``deile/tests/runtime/`` para não onerar a suíte inteira.
    """
    from deile.runtime import instance_state as mod

    runtime_dir = tmp_path / "run"
    monkeypatch.setenv("DEILE_RUNTIME_DIR", str(runtime_dir))
    mod.reset_instance_state()
    try:
        yield runtime_dir
    finally:
        mod.reset_instance_state()
