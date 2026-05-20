"""Minimal ``.gitignore`` matching helpers shared by file tools.

This is intentionally NOT a full ``gitignore`` engine (no negation, no
``**`` semantics) — it preserves the historical behaviour of
``ListFilesTool`` which used ``fnmatch`` with three matchers per pattern:
literal match, directory-child match, and prefix-component match.

Lifted from ``file_tools.py`` so other tools that want to respect
``.gitignore`` (e.g. ``SearchTool``) can reuse the same matcher instead
of growing a second parallel exclude list.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List


def load_gitignore_patterns(working_directory: Path) -> List[str]:
    """Read non-comment, non-empty lines from ``<working_directory>/.gitignore``.

    Returns an empty list when the file is missing or unreadable — callers
    interpret an empty list as "no filtering". Reading errors are silently
    swallowed to match the legacy behaviour from ``ListFilesTool``.
    """
    gitignore_path = working_directory / ".gitignore"
    if not gitignore_path.exists():
        return []
    try:
        content = gitignore_path.read_text(encoding="utf-8")
    except Exception:
        return []
    return [
        line for raw in content.splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]


def should_ignore_path(
    file_path: Path, patterns: List[str], working_directory: Path,
) -> bool:
    """Return True when ``file_path`` (relative to ``working_directory``) matches
    any of the ``patterns`` under the legacy three-rule matcher.

    Rules (per pattern):
    1. Literal ``fnmatch`` against the relative path.
    2. ``<pattern>/*`` match (a directory entry's children).
    3. Component-prefix match (path lives under a matched ancestor).
    """
    if not patterns:
        return False
    try:
        relative_path = file_path.relative_to(working_directory)
    except ValueError:
        return False
    path_str = str(relative_path).replace("\\", "/")
    parts = path_str.split("/")
    for pattern in patterns:
        clean_pattern = pattern.rstrip("/")
        if fnmatch.fnmatch(path_str, clean_pattern):
            return True
        if fnmatch.fnmatch(path_str, clean_pattern + "/*"):
            return True
        for i in range(len(parts)):
            partial_path = "/".join(parts[: i + 1])
            if fnmatch.fnmatch(partial_path, clean_pattern):
                return True
    return False
