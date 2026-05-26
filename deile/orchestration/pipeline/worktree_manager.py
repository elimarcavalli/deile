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
from typing import Optional, Sequence

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

        # A partir daqui, qualquer falha precisa **reverter** o ``copytree``
        # acima — sem rollback, a próxima tick reusaria a worktree
        # silenciosamente quebrada (apenas o ``.git`` copiado, sem checkout
        # da branch correta — pilar 03 §9 rollback em operação multi-step).
        try:
            # Inside the copy, point origin at the parent base_repo so commits
            # land back there (and from there get pushed to the upstream forge
            # — GitHub or GitLab — by the pipeline).
            await self._git_in(target, "remote", "set-url", "origin", str(self.base_repo))

            # Ensure a ``forge`` remote pointing directly at the upstream forge
            # exists so ``gh pr create`` / ``glab mr create`` and ``git push
            # forge <branch>`` work from within the worktree. We discover the
            # URL from the base repo's existing ``forge`` (or legacy ``github``)
            # remote, falling back to ``origin``. Errors são não-fatais.
            await self._ensure_forge_remote(target)

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
        except BaseException:
            # Rollback do ``copytree``. ``BaseException`` (não só ``Exception``)
            # para também limpar em ``CancelledError`` / ``KeyboardInterrupt`` —
            # o rollback é re-raised abaixo, então a semântica não muda.
            logger.warning(
                "create_branch_worktree falhou após copytree; removendo "
                "worktree parcial em %s para evitar reuso silenciosamente quebrado",
                target,
            )
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            raise
        return Worktree(path=target, branch=branch, base_repo=self.base_repo)

    async def cleanup_merged_branches(self, merged_branches: Sequence[str]) -> int:
        """Delete on-disk worktrees whose branch is in *merged_branches* (gap #26).

        The caller supplies the set of branch names that have already been
        merged on the remote — this module no longer talks to GitHub. Any
        local ``.worktrees/`` sub-directory whose relative path matches one
        of those branches is removed. Returns the number of worktrees
        deleted.

        ``merged_branches`` is typed as :class:`~typing.Sequence` (not
        ``Iterable``) because it is iterated once to build a lookup set —
        a single-use generator would silently empty before the rglob walk
        begins.  Callers should pass a list/tuple.

        Best-effort: individual remove errors are logged at WARNING, never
        raised.
        """
        merged_set = frozenset(b for b in merged_branches if b)
        if not merged_set or not self.branches_dir.exists():
            return 0

        deleted = 0
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
            if branch_name in merged_set:
                try:
                    await asyncio.to_thread(shutil.rmtree, candidate, ignore_errors=False)
                    logger.info("cleaned up merged worktree: %s (branch=%s)", candidate, branch_name)
                    deleted += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cleanup_merged_branches: failed to remove %s: %s", candidate, exc)

        return deleted

    # Fragmentos de nome de host que identificam uma URL de forge real — usados
    # para distinguir um remote que deve ser propagado para a worktree de um
    # path local. A lista cobre os hosts cloud padrão e os padrões self-hosted
    # mais comuns. O fragmento ``"git."`` foi removido propositalmente: ele é
    # demasiado permissivo (qualquer URL contendo ``git.algo.com`` casaria,
    # incluindo repositórios internos legítimos que NÃO são forges). Operadores
    # com hosts exóticos devem configurar ``DEILE_GITHUB_HOST`` ou
    # ``DEILE_GITLAB_HOST`` explicitamente.
    _FORGE_HOST_HINTS = ("github.com", "gitlab.com", "gitlab.", "ghe.")

    async def _ensure_forge_remote(self, worktree: Path) -> None:
        """Add or update the ``forge`` remote (and legacy ``github`` alias).

        Priority order for discovering the upstream URL:
        1. The ``forge`` remote of the base repo (if it already exists).
        2. The legacy ``github`` remote of the base repo (backwards compat).
        3. The ``origin`` remote of the base repo (may be a local path).
        4. No-op: log a warning and leave the worktree without a forge remote.

        When the resolved URL points at a recognisable forge host
        (``github.com`` / ``gitlab.com`` / declared custom hosts), both
        ``forge`` and (for GitHub URLs only) the legacy ``github`` alias
        are configured. This is best-effort: failures are logged at
        WARNING but never raised.
        """
        # Prefer the new ``forge`` remote, then the legacy ``github`` remote,
        # then ``origin``. Each lookup is a cheap subprocess call.
        forge_url = ""
        for remote_name in ("forge", "github", "origin"):
            rc, candidate, _ = await self._git_in_capture(
                self.base_repo, "remote", "get-url", remote_name,
            )
            if rc == 0 and candidate.strip():
                forge_url = candidate.strip()
                break

        if not forge_url:
            logger.warning(
                "_ensure_forge_remote: could not determine upstream forge URL for %s; "
                "worktree will lack a 'forge' remote — the agent may need to configure it.",
                worktree,
            )
            return

        # Only add if it looks like a real forge URL (not a local path).
        if not any(h in forge_url for h in self._FORGE_HOST_HINTS):
            logger.debug(
                "_ensure_forge_remote: base repo origin %r is not a forge URL; skipping",
                forge_url[:80],
            )
            return

        # Always (re)set the canonical ``forge`` remote.
        await self._set_remote(worktree, "forge", forge_url)
        # GH compatibility: keep the legacy ``github`` alias when the URL
        # actually points at GitHub. Scripts/instructions that used
        # ``git push github <branch>`` keep working without modification.
        if "github.com" in forge_url:
            await self._set_remote(worktree, "github", forge_url)

    async def _set_remote(self, worktree: Path, name: str, url: str) -> None:
        """Idempotently set ``<worktree> remote <name>`` to ``url``.

        If the remote does not exist, add it; if it does and the URL
        differs, update it; otherwise no-op. Best-effort — errors are
        logged at WARNING (the forge remote is convenience, not required).
        """
        rc_existing, existing_url, _ = await self._git_in_capture(
            worktree, "remote", "get-url", name,
        )
        try:
            if rc_existing == 0:
                if existing_url.strip() != url:
                    await self._git_in(worktree, "remote", "set-url", name, url)
                    logger.debug("updated %r remote in %s to %s", name, worktree, url[:80])
            else:
                await self._git_in(worktree, "remote", "add", name, url)
                logger.debug("added %r remote %s to %s", name, url[:80], worktree)
        except WorktreeError as exc:
            logger.warning(
                "_set_remote: could not set %r remote in %s: %s", name, worktree, exc,
            )

    # Backwards-compat alias (issue #297): existing test code and external
    # scripts may still call ``_ensure_github_remote`` directly. It now
    # delegates to the forge-aware path verbatim.
    async def _ensure_github_remote(self, worktree: Path) -> None:
        await self._ensure_forge_remote(worktree)

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
