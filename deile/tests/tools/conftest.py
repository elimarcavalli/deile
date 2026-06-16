"""Shared fixtures for tools tests.

Resets the Settings singleton around tests that manipulate DEILE_* env vars
so that monkeypatch.setenv changes are actually picked up.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    from deile.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


@pytest.fixture(autouse=True)
def _reset_bridge_executor():
    """Reset o ``_BRIDGE_EXECUTOR`` module-level entre testes.

    Evita leak de estado entre testes que materializam o executor
    (notavelmente ``test_run_coro_sync_inside_event_loop``) — sem isso
    o pool de threads sobrevive ao teste e a asserção
    ``fc_mod._BRIDGE_EXECUTOR is not None`` poderia passar por estado
    residual de outro teste em vez do que o teste atual provou.
    """
    from deile.tools import function_call as fc_mod

    yield
    if fc_mod._BRIDGE_EXECUTOR is not None:
        fc_mod._BRIDGE_EXECUTOR.shutdown(wait=False)
        fc_mod._BRIDGE_EXECUTOR = None


# Repo root — used by fixtures that need a safe-root-compatible temp directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def repo_tmp_path():
    """Yield a temporary directory created inside the git repo root.

    Because _assert_safe_root accepts paths under the git repo root, tests
    that exercise the path-containment guard must pass a path within the repo
    (not in /tmp, which is world-writable and therefore excluded from safe
    roots).  This fixture creates such a directory and removes it on teardown.
    """
    tmp_dir = tempfile.mkdtemp(dir=_REPO_ROOT, prefix=".test_tmp_")
    try:
        yield Path(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
