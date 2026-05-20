"""Shared helpers for extracting a file-path argument from ``ToolContext``.

The file tools (`ReadFileTool`, `WriteFileTool`, `EditFileTool`,
`DeleteFileTool`) all accept the path under several synonyms because the
LLM caller does not always pick the canonical key. Each tool used to
re-implement the same fallback chain (``file_path`` → ``path`` →
``filename`` → ``file`` → ``filepath``) inline; centralising the chain
here keeps the vocabulary in one place so a new alias is added once.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

#: Canonical fallback order for the file-path argument shared by every
#: file tool. Order: most-explicit first, generic last.
FILE_PATH_ALIASES: Sequence[str] = (
    "file_path",
    "path",
    "filename",
    "file",
    "filepath",
)


def extract_file_path_arg(
    parsed_args: Mapping[str, object],
    aliases: Sequence[str] = FILE_PATH_ALIASES,
) -> Optional[str]:
    """Return the first non-empty value found under ``aliases`` in ``parsed_args``.

    Returns ``None`` when no alias yields a truthy string. Non-string truthy
    values are passed through so callers can decide how to coerce them — the
    historical behaviour is to forward whatever the LLM emitted and let the
    path-resolution stage reject non-string types.
    """
    for key in aliases:
        value = parsed_args.get(key)
        if value:
            return value  # type: ignore[return-value]
    return None
