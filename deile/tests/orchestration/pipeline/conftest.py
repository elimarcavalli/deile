"""Shared fixtures for pipeline orchestration tests.

Provides ``repo_tmp_path`` — a temporary directory created inside the git
repo root so that ``_assert_safe_root`` accepts it (``/tmp`` is excluded
from safe roots because it is world-writable).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]  # tests/orchestration/pipeline → repo root


@pytest.fixture()
def repo_tmp_path():
    """Yield a temporary directory inside the git repo root (safe root)."""
    tmp_dir = tempfile.mkdtemp(dir=_REPO_ROOT, prefix=".test_tmp_")
    try:
        yield Path(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def repo_git_tmp(repo_tmp_path, monkeypatch) -> Path:
    """Create a minimal git repo inside the git repo root.

    Sets DEILE_PIPELINE_BASE_PATH so ``resolve_base_path()`` picks it up,
    and also resets the Settings singleton so the env var is visible.
    """
    subprocess.run(
        ["git", "init", str(repo_tmp_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    (repo_tmp_path / "deile.py").write_text("# marker\n")
    monkeypatch.setenv("DEILE_PIPELINE_BASE_PATH", str(repo_tmp_path))
    # Reset singleton so the new env var is picked up.
    from deile.config.settings import reset_settings
    reset_settings()
    yield repo_tmp_path
    reset_settings()
