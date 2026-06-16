"""Directory-listing helpers for `ListFilesTool`.

Pure functions extracted from `file_tools.py` to keep
`ListFilesTool.execute_sync` (formerly cyclomatic complexity 47) a thin
orchestrator. None of these touch tool execution context — they operate
on plain `Path` objects:

* ``_load_gitignore_patterns`` — read a ``.gitignore`` into a pattern list.
* ``_should_ignore`` — match a path against those patterns.
* ``_collect_entries`` — walk a resolved path into sorted entry dicts.
* ``_render_tree`` — turn entry dicts into the rich tree display string.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Display caps for `_render_tree`. Promoted from inline literals so the two
# branches that compute the truncated views (and the "... e mais N itens"
# remainder line) read from a single source of truth.
_MAX_DIRS_SHOWN = 8
_MAX_FILES_SHOWN = 15


def _load_gitignore_patterns(working_directory: Path) -> List[str]:
    """Carrega padrões do .gitignore"""
    gitignore_path = working_directory / ".gitignore"
    patterns: List[str] = []

    if gitignore_path.exists():
        try:
            content = gitignore_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                # Ignora linhas vazias e comentários
                if line and not line.startswith("#"):
                    patterns.append(line)
        except (OSError, UnicodeDecodeError) as exc:
            # If the file can't be read or decoded, continue with no patterns;
            # log so the silent fallback is at least diagnosable.
            logger.debug("failed to read .gitignore at %s: %s", gitignore_path, exc)

    return patterns


def _should_ignore(
    file_path: Path, patterns: List[str], working_directory: Path
) -> bool:
    """Verifica se um arquivo deve ser ignorado baseado nos padrões do .gitignore"""
    if not patterns:
        return False

    try:
        # Caminho relativo ao diretório de trabalho
        relative_path = file_path.relative_to(working_directory)
        path_str = str(relative_path).replace("\\", "/")

        # Verifica cada padrão
        for pattern in patterns:
            # Remove / no final para diretórios
            clean_pattern = pattern.rstrip("/")

            # Verifica match direto
            if fnmatch.fnmatch(path_str, clean_pattern):
                return True

            # Verifica match com padrão de diretório
            if fnmatch.fnmatch(path_str, clean_pattern + "/*"):
                return True

            # Verifica se está dentro de um diretório ignorado
            parts = path_str.split("/")
            for i in range(len(parts)):
                partial_path = "/".join(parts[: i + 1])
                if fnmatch.fnmatch(partial_path, clean_pattern):
                    return True

        return False
    except ValueError:
        # Se não conseguir calcular caminho relativo, não ignora
        return False


def _collect_entries(
    full_path: Path,
    working_directory: Path,
    *,
    recursive: Union[bool, str],
    show_hidden: bool,
    pattern: Optional[str],
) -> List[Dict[str, Any]]:
    """Walk ``full_path`` into a sorted list of entry dicts.

    Handles both the single-file case (one entry, no ``.gitignore``
    filtering) and the directory case (glob/rglob walk with hidden-file
    and ``.gitignore`` filtering). ``recursive`` may arrive as a native
    bool or a stringified ("True"/"False") flag from the LLM.
    """
    files_info: List[Dict[str, Any]] = []

    if full_path.is_file():
        # Se é um arquivo específico, não aplica filtros do .gitignore
        stat = full_path.stat()
        files_info.append(
            {
                "name": full_path.name,
                "type": "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "path": str(full_path.relative_to(working_directory)),
            }
        )
        return files_info

    # Se é um diretório, carrega padrões do .gitignore
    gitignore_patterns = _load_gitignore_patterns(working_directory)

    # ``recursive`` comes from the LLM as either a native bool or a
    # stringified ("True"/"False"); coerce so the rglob/glob branch is
    # reachable. ``pattern`` is used raw — coercion of stringified globs
    # (e.g. quoted ``"*.py"``) is a separate concern left for a followup.
    recursive_flag = recursive
    if isinstance(recursive_flag, str):
        recursive_flag = recursive_flag.strip().lower() in {"true", "1", "yes"}

    # `iterdir()` is intentional (not `glob("*")`) for the non-recursive,
    # no-pattern path: it skips the glob-engine overhead for the common case.
    if recursive_flag:
        entries = full_path.rglob(pattern or "*")
    elif pattern:
        entries = full_path.glob(pattern)
    else:
        entries = full_path.iterdir()

    for entry in entries:
        # Pula arquivos ocultos se não solicitado
        if not show_hidden and entry.name.startswith("."):
            continue

        # Verifica se deve ser ignorado pelo .gitignore
        if _should_ignore(entry, gitignore_patterns, working_directory):
            continue

        try:
            stat = entry.stat()
            # ``relative_to`` raises ValueError when ``entry`` isn't lexically
            # under ``working_directory`` (symlinks pointing outside the
            # tree, or rglob yielding a differently-anchored path). Without
            # catching ValueError the exception escaped and dropped ALL
            # results — the loop aborted instead of skipping the offender.
            try:
                rel = str(entry.relative_to(working_directory))
            except ValueError:
                rel = str(entry)
            files_info.append(
                {
                    "name": entry.name,
                    "type": "directory" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                    "modified": stat.st_mtime,
                    "path": rel,
                }
            )
        except (PermissionError, OSError):
            # Pula arquivos sem permissão
            continue

    # Ordena por nome
    files_info.sort(key=lambda x: x["name"].lower())
    return files_info


def _render_tree(target_path: str, files_info: List[Dict[str, Any]]) -> str:
    """Render the rich tree display for a directory listing.

    Caps at ``_MAX_DIRS_SHOWN`` directories and ``_MAX_FILES_SHOWN`` files;
    a trailing "... e mais N itens" line accounts for the remainder.
    """
    rich_display_lines = [f"● list_files({target_path})", "⎿ Estrutura do projeto:"]

    # Cria tree structure visual
    if files_info:
        # Agrupa por diretórios e arquivos
        dirs = [f for f in files_info if f["type"] == "directory"]
        files = [f for f in files_info if f["type"] == "file"]

        shown_dirs = dirs[:_MAX_DIRS_SHOWN]
        shown_files = files[:_MAX_FILES_SHOWN]

        rich_display_lines.append(f"   {target_path}/")

        # Mostra diretórios primeiro (máximo _MAX_DIRS_SHOWN)
        for i, dir_info in enumerate(shown_dirs):
            is_last_dir = i == len(shown_dirs) - 1 and not files
            prefix = "└── " if is_last_dir else "├── "
            rich_display_lines.append(f"   {prefix}📁 {dir_info['name']}/")

        # Mostra arquivos (máximo _MAX_FILES_SHOWN)
        for i, file_info in enumerate(shown_files):
            is_last_file = i == len(shown_files) - 1
            prefix = "└── " if is_last_file else "├── "
            rich_display_lines.append(f"   {prefix}📄 {file_info['name']}")

        # Indica se há mais itens (dirs hidden + files hidden).
        total_remaining = (len(dirs) - len(shown_dirs)) + (
            len(files) - len(shown_files)
        )
        if total_remaining > 0:
            rich_display_lines.append(f"   └── ... e mais {total_remaining} itens")
    else:
        rich_display_lines.append("   (pasta vazia)")

    # FORÇA quebras de linha duplas para garantir formatação
    return "\n".join(rich_display_lines) + "\n"
