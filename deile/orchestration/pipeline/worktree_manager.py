"""Manage isolated working copies under ``.worktrees/<branch>``.

The convention codified here (specified by the project owner):

1. Pull ``main`` of the *invoked* repository.
2. Ensure ``.worktrees/main`` exists as a clean clone of the same repo; pull it.
3. Copy ``.worktrees/main`` to ``.worktrees/<branch>`` (filesystem copy — *not*
   ``git worktree add``; the spec is a fresh clone-like layout).
4. Inside ``<branch>``, create the git branch and let the caller mutate.

This module is consumed by both the autonomous pipeline (when DEILE/Claude pick
up an issue) and the standalone ``WorktreeTool`` exposed to other tools.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from deile.core.exceptions import DEILEError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Worktree:
    """Result of a worktree setup."""

    path: Path
    branch: str
    base_repo: Path


class WorktreeError(DEILEError):
    """Raised when worktree setup fails."""


class WorktreeManager:
    """Build per-branch sandboxes under ``.worktrees/`` of ``base_repo``.

    Parameters
    ----------
    base_repo:
        Directory of the source git repository. ``.worktrees/`` is created
        inside this directory.
    main_branch:
        Name of the integration branch (default ``main``).
    """

    def __init__(
        self,
        base_repo: Path,
        *,
        main_branch: str = "main",
        subdir: Optional[str] = None,
    ) -> None:
        """Initialize worktree manager.

        ``subdir`` namespaces all per-branch worktrees under
        ``.worktrees/<subdir>/<branch>``. ``.worktrees/main`` (the
        always-clean clone of main) is shared across subdirs to save disk
        and keep ``ensure_main`` cheap. Pass ``subdir=monitor_id`` to keep
        parallel monitors from colliding.
        """
        self.base_repo = Path(base_repo).resolve()
        if not (self.base_repo / ".git").exists():
            raise WorktreeError(f"{self.base_repo} is not a git repository")
        self.main_branch = main_branch
        self.subdir = subdir
        self.worktrees_dir = self.base_repo / ".worktrees"
        if subdir is not None:
            self.branches_dir = self.worktrees_dir / subdir
        else:
            self.branches_dir = self.worktrees_dir

    @property
    def main_worktree(self) -> Path:
        # Shared across subdirs: same clean main clone.
        return self.worktrees_dir / "main"

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def ensure_main(self) -> Path:
        """Make sure ``.worktrees/main`` exists and is up-to-date.

        On first run this clones the base repo. On subsequent runs it pulls.
        """
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        if not (self.main_worktree / ".git").exists():
            logger.info("cloning %s -> %s", self.base_repo, self.main_worktree)
            await self._git("clone", str(self.base_repo), str(self.main_worktree))
        await self._git_in(self.main_worktree, "fetch", "origin")
        await self._git_in(self.main_worktree, "checkout", self.main_branch)
        await self._git_in(self.main_worktree, "pull", "origin", self.main_branch)
        return self.main_worktree

    async def create_branch_worktree(
        self, branch: str, *, force_recreate: bool = False
    ) -> Worktree:
        """Create ``.worktrees/<branch>`` (filesystem copy of main) + checkout branch.

        If the worktree already exists and ``force_recreate`` is False, this
        fast-paths and just returns the existing path.  When ``force_recreate``
        is True the existing worktree is deleted first so the retry starts
        clean (gap #12: avoids contamination from a previous failed run).
        """
        if not branch or branch == self.main_branch:
            raise WorktreeError(f"branch must be a non-main branch name, got {branch!r}")
        await self.ensure_main()
        target = self.branches_dir / branch
        if (target / ".git").exists():
            if force_recreate:
                logger.info("force_recreate=True: removing stale worktree %s", target)
                await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            else:
                logger.info("worktree %s already exists; reusing", target)
                return Worktree(path=target, branch=branch, base_repo=self.base_repo)

        target.parent.mkdir(parents=True, exist_ok=True)
        logger.info("copying %s -> %s", self.main_worktree, target)
        # `cp -r` mirrors the spec literally; shutil.copytree refuses an
        # existing destination, which we've already ruled out above.
        await asyncio.to_thread(shutil.copytree, self.main_worktree, target,
                                symlinks=False, ignore=None)

        # Inside the copy, point origin at the parent base_repo so commits
        # land back there (and from there get pushed to GitHub by the pipeline).
        await self._git_in(target, "remote", "set-url", "origin", str(self.base_repo))

        # Create / switch to the feature branch.
        rc, _, err = await self._git_in_capture(target, "checkout", "-b", branch)
        if rc != 0:
            # Branch may already exist; try plain checkout and surface both errors on failure.
            logger.debug("checkout -b %s failed (%s); trying plain checkout", branch, err.strip()[:200])
            rc2, _, err2 = await self._git_in_capture(target, "checkout", branch)
            if rc2 != 0:
                raise WorktreeError(
                    f"could not create or checkout branch {branch!r} in {target}: "
                    f"create-err={err.strip()[:200]!r} checkout-err={err2.strip()[:200]!r}"
                )
        return Worktree(path=target, branch=branch, base_repo=self.base_repo)

    async def cleanup_merged_branches(self, repo: str) -> int:
        """Delete on-disk worktrees whose corresponding PR has been merged (gap #26).

        Queries ``gh pr list --state merged`` for the given *repo* and removes
        any local ``.worktrees/`` sub-directory whose branch name appears in
        the merged PR list.  Returns the number of worktrees deleted.

        Best-effort: individual errors are logged at WARNING, never raised.
        """
        import json as _json
        import shutil as _shutil

        deleted = 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "list",
                "--repo", repo,
                "--state", "merged",
                "--limit", "100",
                "--json", "headRefName",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, _ = await proc.communicate()
            data = _json.loads((stdout_b or b"").decode("utf-8", "replace") or "[]")
            merged_branches = {item.get("headRefName", "") for item in data if item.get("headRefName")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup_merged_branches: gh pr list failed: %s", exc)
            return 0

        if not self.branches_dir.exists():
            return 0

        # Walk recursively: branches are stored at branches_dir / branch_name
        # where branch_name can contain path separators (e.g. "auto/issue-42").
        # We need the path relative to branches_dir to reconstruct the branch name.
        for candidate in self.branches_dir.rglob("*"):
            if not candidate.is_dir():
                continue
            if not (candidate / ".git").exists():
                continue
            try:
                branch_name = str(candidate.relative_to(self.branches_dir))
            except ValueError:
                continue
            if branch_name in merged_branches:
                try:
                    await asyncio.to_thread(_shutil.rmtree, candidate, ignore_errors=False)
                    logger.info("cleaned up merged worktree: %s (branch=%s)", candidate, branch_name)
                    deleted += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cleanup_merged_branches: failed to remove %s: %s", candidate, exc)

        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _git(*args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        if (proc.returncode or 0) != 0:
            raise WorktreeError(
                f"git {' '.join(args)} failed: {stderr_b.decode('utf-8', 'replace').strip()[:300]}"
            )

    @staticmethod
    async def _git_in(cwd: Path, *args: str) -> None:
        rc, _, err = await WorktreeManager._git_in_capture(cwd, *args)
        if rc != 0:
            raise WorktreeError(
                f"git -C {cwd} {' '.join(args)} failed: {err.strip()[:300]}"
            )

    @staticmethod
    async def _git_in_capture(cwd: Path, *args: str):
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", "replace"),
            stderr_b.decode("utf-8", "replace"),
        )
