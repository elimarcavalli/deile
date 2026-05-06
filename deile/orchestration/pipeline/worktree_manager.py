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
import subprocess
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

    def __init__(self, base_repo: Path, *, main_branch: str = "main") -> None:
        self.base_repo = Path(base_repo).resolve()
        if not (self.base_repo / ".git").exists():
            raise WorktreeError(f"{self.base_repo} is not a git repository")
        self.main_branch = main_branch
        self.worktrees_dir = self.base_repo / ".worktrees"

    @property
    def main_worktree(self) -> Path:
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

    async def create_branch_worktree(self, branch: str) -> Worktree:
        """Create ``.worktrees/<branch>`` (filesystem copy of main) + checkout branch.

        If the worktree already exists, this fast-paths and just returns the
        existing path.
        """
        if not branch or branch == self.main_branch:
            raise WorktreeError(f"branch must be a non-main branch name, got {branch!r}")
        await self.ensure_main()
        target = self.worktrees_dir / branch
        if (target / ".git").exists():
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
            # Branch may already exist locally; try a plain checkout.
            rc2, _, err2 = await self._git_in_capture(target, "checkout", branch)
            if rc2 != 0:
                raise WorktreeError(
                    f"could not create branch {branch!r} in {target}: {err or err2}"
                )
        return Worktree(path=target, branch=branch, base_repo=self.base_repo)

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
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        if (proc.returncode or 0) != 0:
            raise WorktreeError(
                f"git -C {cwd} {' '.join(args)} failed: "
                f"{stderr_b.decode('utf-8', 'replace').strip()[:300]}"
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
