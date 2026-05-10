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

import tempfile
from pathlib import Path
from typing import Optional


def _assert_safe_root(path: Path) -> None:
    """Raise ValueError if *path* is outside all known safe roots.

    Safe roots are:
    - The current user's home directory (``Path.home()``).
    - The git repository root found by walking up from ``Path.cwd()``
      (i.e. the first ancestor directory that contains a ``.git`` entry).
    - The system temporary directory (``tempfile.gettempdir()``), which is
      used by pytest fixtures and other trusted runtime scratch space.

    This enforces Pilar 08: arbitrary caller-supplied paths must be contained
    within a trusted directory before any filesystem access.
    """
    safe_roots = [Path.home(), Path(tempfile.gettempdir()).resolve()]
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists():
            safe_roots.append(ancestor)
            break

    for root in safe_roots:
        try:
            path.relative_to(root)
            return  # contained — OK
        except ValueError:
            continue

    raise ValueError(
        f"Path '{path}' is outside all safe roots {[str(r) for r in safe_roots]}. "
        "Supply a path inside your home directory or the current git repository."
    )


def resolve_base_path(override: Optional[str] = None) -> Path:
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
        if (ancestor / ".git").is_dir() and (ancestor / "deile.py").is_file():
            return ancestor
    return cwd
