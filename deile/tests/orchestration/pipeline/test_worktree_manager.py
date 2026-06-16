"""Unit tests for WorktreeManager — uses real git in tmp_path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from deile.orchestration.pipeline.worktree_manager import (
    Worktree,
    WorktreeError,
    WorktreeManager,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit on main."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# fake\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


class TestWorktreeManagerCtor:
    def test_rejects_non_repo(self, tmp_path):
        with pytest.raises(WorktreeError):
            WorktreeManager(tmp_path)

    def test_accepts_repo(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        assert wm.base_repo == fake_repo.resolve()
        assert wm.worktrees_dir == fake_repo.resolve() / ".worktrees"


class TestEnsureMain:
    async def test_clones_main_on_first_run(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        path = await wm.ensure_main()
        assert path.exists()
        assert (path / ".git").exists()
        assert (path / "README.md").exists()

    async def test_idempotent(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        p1 = await wm.ensure_main()
        p2 = await wm.ensure_main()
        assert p1 == p2


class TestCreateBranchWorktree:
    async def test_rejects_main_branch(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        with pytest.raises(WorktreeError):
            await wm.create_branch_worktree("main")

    async def test_rejects_empty_branch(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        with pytest.raises(WorktreeError):
            await wm.create_branch_worktree("")

    async def test_creates_branch_copy(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        wt = await wm.create_branch_worktree("feat/x")
        assert isinstance(wt, Worktree)
        assert wt.branch == "feat/x"
        assert (wt.path / ".git").exists()
        assert (wt.path / "README.md").exists()
        # Verify branch is checked out.
        out = subprocess.check_output(
            ["git", "-C", str(wt.path), "branch", "--show-current"]
        )
        assert out.strip() == b"feat/x"

    async def test_idempotent_reuses_existing(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        wt1 = await wm.create_branch_worktree("feat/y")
        # Simulate user-edited file in the worktree.
        (wt1.path / "marker.txt").write_text("preserved\n")
        wt2 = await wm.create_branch_worktree("feat/y")
        assert wt1.path == wt2.path
        assert (wt2.path / "marker.txt").exists(), "existing worktree must not be wiped"

    async def test_remote_origin_points_back_to_base_repo(self, fake_repo):
        wm = WorktreeManager(fake_repo)
        wt = await wm.create_branch_worktree("feat/origin-check")
        out = subprocess.check_output(
            ["git", "-C", str(wt.path), "remote", "get-url", "origin"]
        )
        assert out.strip().decode() == str(fake_repo.resolve())


class TestForgeHostHints:
    """Verifica que _FORGE_HOST_HINTS não inclui o fragmento ``"git."`` (muito permissivo)."""

    def test_git_dot_not_in_hints(self):
        """``"git."`` foi removido das hints — URLs como git.empresa.com NÃO devem casar."""
        assert "git." not in WorktreeManager._FORGE_HOST_HINTS

    def test_standard_cloud_hosts_still_in_hints(self):
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert "github.com" in hints
        assert "gitlab.com" in hints

    def test_enterprise_prefixes_still_in_hints(self):
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert "ghe." in hints
        assert "gitlab." in hints

    def test_git_empresa_url_does_not_match(self):
        """URL genérica ``git.empresa.com/x/y`` NÃO deve ser reconhecida como forge."""
        url = "https://git.empresa.com/grupo/projeto"
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert not any(
            h in url for h in hints
        ), f"'git.empresa.com' não deveria casar nenhum hint; hints={hints}"

    def test_github_url_still_matches(self):
        url = "https://github.com/owner/repo"
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert any(h in url for h in hints)

    def test_gitlab_url_still_matches(self):
        url = "https://gitlab.com/group/project"
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert any(h in url for h in hints)

    def test_ghe_url_still_matches(self):
        url = "https://ghe.empresa.com/owner/repo"
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert any(h in url for h in hints)

    def test_self_hosted_gitlab_url_still_matches(self):
        url = "https://gitlab.empresa.com/group/project"
        hints = WorktreeManager._FORGE_HOST_HINTS
        assert any(h in url for h in hints)
