"""Extension-to-language map and code-block language extraction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

_DEFAULT_EXTENSION_MAP: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ipynb": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
    ".lua": "lua",
    ".r": "r",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
}

# Special-case basenames that have no informative extension (e.g. ``Dockerfile``).
_DEFAULT_BASENAME_MAP: Dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "make",
    "rakefile": "ruby",
    "gemfile": "ruby",
}

# Mirrors the fence pattern in ``deile/ui/markup.py``. Duplicated to avoid
# coupling the skills package to the UI layer.
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]+)\b", re.MULTILINE)


def default_extension_map() -> Dict[str, str]:
    """Return a copy of the built-in extension → language map."""
    return dict(_DEFAULT_EXTENSION_MAP)


class LanguageDetector:
    """Maps file extensions and code-block tags to canonical language names."""

    def __init__(
        self,
        extension_map: Optional[Mapping[str, str]] = None,
        basename_map: Optional[Mapping[str, str]] = None,
    ) -> None:
        merged_ext: Dict[str, str] = dict(_DEFAULT_EXTENSION_MAP)
        if extension_map:
            for k, v in extension_map.items():
                merged_ext[k.lower() if k.startswith(".") else f".{k.lower()}"] = v.lower()
        self._extension_map = merged_ext

        merged_base: Dict[str, str] = dict(_DEFAULT_BASENAME_MAP)
        if basename_map:
            for k, v in basename_map.items():
                merged_base[k.lower()] = v.lower()
        self._basename_map = merged_base

    @property
    def extension_map(self) -> Mapping[str, str]:
        return self._extension_map

    def language_for_path(self, path: str) -> Optional[str]:
        if not path:
            return None
        p = Path(path)
        # Basename override wins (e.g. ``Dockerfile`` has no extension).
        basename = p.name.lower()
        if basename in self._basename_map:
            return self._basename_map[basename]
        suffix = p.suffix.lower()
        if suffix and suffix in self._extension_map:
            return self._extension_map[suffix]
        return None

    def languages_for_paths(self, paths: Iterable[str]) -> List[str]:
        """Return unique, order-preserving languages detected across *paths*."""
        seen: List[str] = []
        for path in paths:
            lang = self.language_for_path(path)
            if lang and lang not in seen:
                seen.append(lang)
        return seen

    def langs_in_code_blocks(self, text: str) -> List[str]:
        """Return language tags from fenced code blocks in *text* (unique, ordered)."""
        if not text:
            return []
        seen: List[str] = []
        for match in _FENCE_RE.finditer(text):
            lang = match.group(1).lower()
            if lang and lang not in seen:
                seen.append(lang)
        return seen
