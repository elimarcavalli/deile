"""Shared base-path resolution for pipeline / worktree tools.

Single source of truth for how DEILE locates the pipeline repository root.
Callers previously duplicated this logic in four files.

Resolution order:
1. Explicit ``override`` argument (used by worktree_tool to honor a CLI flag).
2. ``pipeline.base_path`` setting from :func:`deile.config.settings.get_settings`.
3. Walk CWD ancestors looking for the marker pair ``.git`` directory + ``deile.py`` file.
4. Fall back to the current working directory.
"""
from __future__ import annotations

import logging
from pathlib import Path

from deile.core.exceptions import PathContainmentError

logger = logging.getLogger(__name__)


def _assert_safe_root(path: Path) -> None:
    """Raise PathContainmentError if *path* is outside all known safe roots.

    Safe roots are:
    - The current user's home directory (``Path.home().resolve()``).
    - The git repository root found by walking up from ``Path.cwd()``
      (i.e. the first ancestor directory that contains a ``.git`` entry).

    ``/tmp`` is intentionally excluded: it is world-writable, so any process
    can create ``/tmp/evil/`` to bypass containment.

    This enforces Pilar 08: arbitrary caller-supplied paths must be contained
    within a trusted directory before any filesystem access.
    """
    path = path.resolve()
    home = Path.home().resolve()
    if home == Path("/"):
        raise PathContainmentError(
            "Home directory resolves to filesystem root; path containment cannot be enforced.",
            path=str(path),
            safe_roots=[],
        )
    safe_roots = [home]
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists():  # also matches worktree .git files
            try:
                # Skip world-writable dirs: an attacker could create /tmp/.git to
                # elevate /tmp into a safe root if the process CWD happens to be /tmp.
                if not (ancestor.stat().st_mode & 0o002):
                    safe_roots.append(ancestor)
            except OSError:
                logger.debug("stat() failed for git root %s; not adding to safe roots", ancestor, exc_info=True)
            break

    for root in safe_roots:
        try:
            path.relative_to(root)
            return  # contained — OK
        except ValueError:
            continue

    raise PathContainmentError(
        f"Path '{path}' is outside all safe roots {[str(r) for r in safe_roots]}. "
        "Supply a path inside your home directory or the current git repository.",
        path=str(path),
        safe_roots=safe_roots,
    )


def resolve_base_path(override: str | None = None) -> Path:
    if override:
        resolved = Path(override).resolve()
        _assert_safe_root(resolved)
        return resolved
    from deile.config.settings import get_settings

    s = get_settings()
    if s.pipeline_base_path:
        resolved = s.pipeline_base_path.resolve()
        _assert_safe_root(resolved)
        return resolved
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists() and (ancestor / "deile.py").is_file():
            return ancestor
    # cwd is trusted by definition (operator chose it); no _assert_safe_root needed.
    return cwd
