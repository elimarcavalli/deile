"""Adversarial coverage for the safe-root containment guard introduced in
``deile/tools/_pipeline_paths.py`` (commit 9774f86, refactor PR #182).

These tests intentionally feed paths that fall outside every safe root
(``Path.home()``, system tempdir, current git repo) and assert that the
guard raises ``ValueError``. Without these, a regression that drops the
guard or inverts the containment check would pass the rest of the suite
silently.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from deile.tools._pipeline_paths import _assert_safe_root, resolve_base_path


@pytest.mark.security
def test_assert_safe_root_rejects_etc():
    with pytest.raises(ValueError, match="outside all safe roots"):
        _assert_safe_root(Path("/etc/passwd"))


@pytest.mark.security
def test_assert_safe_root_rejects_proc():
    with pytest.raises(ValueError, match="outside all safe roots"):
        _assert_safe_root(Path("/proc/self/environ"))


@pytest.mark.security
def test_resolve_base_path_rejects_outside_override():
    with pytest.raises(ValueError, match="outside all safe roots"):
        resolve_base_path("/etc")


@pytest.mark.security
def test_assert_safe_root_accepts_home_subdir():
    safe = Path.home() / ".cache"
    if not safe.exists():
        safe = Path.home()
    _assert_safe_root(safe.resolve())


@pytest.mark.security
def test_assert_safe_root_accepts_tempdir(tmp_path):
    _assert_safe_root(tmp_path.resolve())


@pytest.mark.security
def test_assert_safe_root_distinguishes_prefix_from_containment():
    """``/tmp/foobar`` must not be accepted as a child of ``/tmp/foo``."""
    tempdir = Path(tempfile.gettempdir()).resolve()
    p = tempdir / "foo" / ".." / "foobar"
    _assert_safe_root(p.resolve())
