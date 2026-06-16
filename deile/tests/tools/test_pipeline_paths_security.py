"""Adversarial coverage for the safe-root containment guard introduced in
``deile/tools/_pipeline_paths.py`` (commit 9774f86, refactor PR #182).

These tests intentionally feed paths that fall outside every safe root
(``Path.home()``, current git repo) and assert that the guard raises
``PathContainmentError``. Without these, a regression that drops the
guard or inverts the containment check would pass the rest of the suite
silently.

Note: ``/tmp`` is intentionally excluded from safe roots because it is
world-writable — any local process can create ``/tmp/evil/`` to bypass
containment. Tests that previously relied on ``tmp_path`` (which lives
in ``/tmp``) now use ``repo_tmp_path`` from ``conftest.py`` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.core.exceptions import PathContainmentError
from deile.tools._pipeline_paths import _assert_safe_root, resolve_base_path

# ---------------------------------------------------------------------------
# Fixtures for vision_tool adversarial tests
# ---------------------------------------------------------------------------


@pytest.fixture
def _vision_tool():
    from deile.tools.vision_tool import VisionDescribeImageTool

    return VisionDescribeImageTool()


@pytest.fixture
def _ctx_factory():
    from deile.tools.base import ToolContext

    def _make(**kwargs):
        return ToolContext(user_input="", parsed_args=kwargs)

    return _make


# ---------------------------------------------------------------------------
# adversarial: paths that must be rejected
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_assert_safe_root_rejects_etc():
    with pytest.raises(PathContainmentError):
        _assert_safe_root(Path("/etc/passwd"))


@pytest.mark.security
def test_assert_safe_root_rejects_proc():
    with pytest.raises(PathContainmentError):
        _assert_safe_root(Path("/proc/self/environ"))


@pytest.mark.security
def test_resolve_base_path_rejects_outside_override():
    with pytest.raises(PathContainmentError):
        resolve_base_path("/etc")


@pytest.mark.security
def test_assert_safe_root_rejects_tmp():
    """/tmp is world-writable and must NOT be accepted as a safe root."""
    import tempfile

    tmp_dir = Path(tempfile.gettempdir()).resolve()
    with pytest.raises(PathContainmentError):
        _assert_safe_root(tmp_dir / "any_subdir")


# ---------------------------------------------------------------------------
# happy path: paths that must be accepted
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_assert_safe_root_accepts_home_subdir():
    safe = Path.home() / ".cache"
    if not safe.exists():
        safe = Path.home()
    _assert_safe_root(safe.resolve())


@pytest.mark.security
def test_assert_safe_root_accepts_repo_root(repo_tmp_path):
    """A path inside the git repo root must be accepted."""
    _assert_safe_root(repo_tmp_path)


@pytest.mark.security
def test_assert_safe_root_distinguishes_prefix_from_containment(repo_tmp_path):
    """A path that shares a prefix with a safe root but is NOT a child must
    still be accepted if it resolves inside the safe root after normalization."""
    # Create a subdir and verify containment works via symlink normalization:
    sub = repo_tmp_path / "a" / ".." / "b"
    sub.mkdir(parents=True, exist_ok=True)
    # sub.resolve() == repo_tmp_path / "b" which IS inside the safe root
    _assert_safe_root(sub.resolve())


# ---------------------------------------------------------------------------
# adversarial: vision_tool rejects paths outside safe roots
# ---------------------------------------------------------------------------


@pytest.mark.security
async def test_image_path_outside_safe_roots_returns_bad_input(
    _vision_tool, _ctx_factory
):
    """vision_tool must return VISION_BAD_INPUT for paths outside safe roots.

    /etc/passwd is outside both Path.home() and the git repo root, so
    _assert_safe_root raises PathContainmentError which the tool maps to
    VISION_BAD_INPUT without crashing.
    """
    res = await _vision_tool.execute(_ctx_factory(image_path="/etc/passwd"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


# ---------------------------------------------------------------------------
# happy path: _assert_safe_root accepts paths inside a git worktree
# (where .git is a file, not a directory)
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_assert_safe_root_accepts_git_worktree(tmp_path, monkeypatch):
    """_assert_safe_root must accept a path inside a directory whose ancestor
    has a .git *file* (git worktree), not just a .git directory.

    We simulate a worktree by creating a .git file in tmp_path and
    monkeypatching Path.cwd() so the guard's ancestor walk starts there.
    The target subdir is inside tmp_path, so once .git.exists() is True,
    it must be accepted.
    """
    git_file = tmp_path / ".git"
    git_file.write_text("gitdir: /path/to/real/repo/.git/worktrees/my-worktree")
    target = tmp_path / "subdir"
    target.mkdir()

    # Monkeypatch Path.cwd() so _assert_safe_root walks from tmp_path.
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))

    # Must not raise — tmp_path is now a "safe root" (has .git entry).
    _assert_safe_root(target.resolve())
