"""Isolamento de runtime state para o subpacote de testes.

Cada teste roda com um ``DEILE_RUNTIME_DIR`` apontando para um diretório
temporário e com o singleton global de :mod:`deile.runtime.instance_state`
resetado — assim não há contaminação cruzada entre testes nem com o
``~/.deile/run/`` real do dev.

**Path curto obrigatório para Unix sockets (Fase 2 — issue #303):** macOS
limita ``AF_UNIX`` em ~104 chars; o ``tmp_path`` default do pytest fica
muito longo (~150+ chars em ``/private/var/folders/...``). Usamos
``/tmp/dx-<hex>`` para garantir folga e ainda assim ficar dentro de
``$TMPDIR`` que o OS apaga periodicamente.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def short_runtime_dir() -> Path:
    """Cria um diretório com path curto (<60 chars) para Unix sockets."""
    base = Path("/tmp") / f"dx-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(monkeypatch, short_runtime_dir):
    """Redireciona o runtime dir para ``short_runtime_dir`` e reseta o singleton.

    Análogo ao ``_isolate_audit_logger`` no conftest raiz — escopo limitado
    a ``deile/tests/runtime/`` para não onerar a suíte inteira.
    """
    from deile.runtime import instance_state as mod

    runtime_dir = short_runtime_dir / "run"
    monkeypatch.setenv("DEILE_RUNTIME_DIR", str(runtime_dir))
    mod.reset_instance_state()
    try:
        yield runtime_dir
    finally:
        mod.reset_instance_state()
