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

from pathlib import Path
from typing import Optional


def resolve_base_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override).resolve()
    from deile.config.settings import get_settings

    s = get_settings()
    if s.pipeline_base_path:
        return s.pipeline_base_path.resolve()
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").is_dir() and (ancestor / "deile.py").is_file():
            return ancestor
    return cwd
