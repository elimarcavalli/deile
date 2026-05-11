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
