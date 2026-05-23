"""Comando /todo — listar TODO/FIXME/HACK/XXX do código numa tabela Rich.

Varre os arquivos versionados do projeto (via ``git ls-files``), encontra
comentários com os marcadores TODO, FIXME, HACK e XXX (case-insensitive,
palavra completa) e apresenta-os numa tabela Rich agrupada por arquivo,
com linha, autor (via ``git blame``) e idade em dias.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._git_helpers import git_ls_files, resolve_repo_root
from ._shared import wrap_command_errors

logger = logging.getLogger(__name__)

# Marcadores reconhecidos: case-insensitive, palavra completa.
_MARKER_PATTERN = re.compile(
    r"\b(TODO|FIXME|HACK|XXX)\b",
    re.IGNORECASE,
)

# Linhas que são comentários em Python, shell, YAML, toml, etc.
# Cobrimos também C-style (// e /*) pois o repo pode ter JS/TS.
_COMMENT_GUARD = re.compile(
    r"^\s*(#|//|/\*|\*|REM\s)",
)


def _is_comment_line(line: str) -> bool:
    """Heurística: linha começa com prefixo de comentário conhecido."""
    return bool(_COMMENT_GUARD.match(line))


class TodoCommand(DirectCommand):
    """Comando /todo — tabela de débito técnico do código."""

    cli_flag = "--todo"
    cli_help = "List TODO/FIXME/HACK/XXX markers in versioned source files."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig

        config = CommandConfig(
            name="todo",
            description="Lista TODO/FIXME/HACK/XXX do código numa tabela Rich.",
            action="list_todos",
        )
        super().__init__(config)
        self.category = "code_quality"

    @wrap_command_errors("todo", message_template="Falha ao executar /{name}: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
        # `_scan_markers` runs `git ls-files`, opens every tracked file, then
        # runs `git blame --line-porcelain` per file with markers — all
        # blocking subprocess+filesystem I/O. Off-loading the whole scan to a
        # worker thread keeps the event loop responsive (pillar 03 §1).
        repo_root = await asyncio.to_thread(self._resolve_repo_root)
        markers = await asyncio.to_thread(self._scan_markers, repo_root)
        if not markers:
            msg = Text(
                "Nenhum TODO/FIXME/HACK/XXX encontrado 🎉",
                style="green",
            )
            return CommandResult.success_result(msg, "rich", marker_count=0)

        table = self._build_table(markers)
        header = Text(
            f"Encontrei {len(markers)} marcador{'es' if len(markers) > 1 else ''} "
            f"em {len({m['file'] for m in markers})} arquivo{'s' if len({m['file'] for m in markers}) > 1 else ''}:",
            style="bold",
        )
        from rich.console import Group
        return CommandResult.success_result(
            Group(header, table), "rich", marker_count=len(markers)
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_repo_root() -> Path:
        return resolve_repo_root()

    def _scan_markers(self, repo_root: Path) -> list[dict]:
        """Varre os arquivos versionados e retorna lista de marcadores encontrados.

        Cada entrada: {"file": rel_path, "line": int, "marker": str, "author": str, "age_days": int}
        """
        # 1. Lista arquivos versionados
        files = self._git_ls_files(repo_root)

        # 2. Encontra marcadores em cada arquivo
        markers: list[dict] = []
        for rel_path in files:
            abs_path = repo_root / rel_path
            if not abs_path.is_file():
                continue
            # Pula binários (heurística: null byte nas primeiras 8KB)
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # Pula se parece binário
            if "\0" in text[:8192]:
                continue

            file_markers = self._find_markers_in_text(rel_path, text)
            if not file_markers:
                continue

            # 3. Obtém blame info para este arquivo
            blame_info = self._git_blame_file(repo_root, rel_path)

            for m in file_markers:
                line_no = m["line"]
                blame = blame_info.get(line_no, {})
                author = blame.get("author", "?")
                age_days = self._calc_age_days(blame.get("committer_time"))
                markers.append({
                    "file": rel_path,
                    "line": line_no,
                    "marker": m["marker"],
                    "author": author,
                    "age_days": age_days,
                })

        return markers

    @staticmethod
    def _git_ls_files(repo_root: Path) -> list[str]:
        return git_ls_files(repo_root)

    @staticmethod
    def _find_markers_in_text(rel_path: str, text: str) -> list[dict]:
        """Encontra marcadores em ``text``, retorna lista de {"line": int, "marker": str}."""
        found: list[dict] = []
        for line_no, line in enumerate(text.split("\n"), start=1):
            # Só considera linhas que parecem comentários
            if not _is_comment_line(line):
                continue
            for match in _MARKER_PATTERN.finditer(line):
                found.append({"line": line_no, "marker": match.group(1).upper()})
        return found

    @staticmethod
    def _git_blame_file(repo_root: Path, rel_path: str) -> dict[int, dict]:
        """Executa ``git blame --line-porcelain`` e retorna mapa linha → info de autor.

        Retorna: {line_no: {"author": str, "committer_time": int or None}, ...}
        """
        blame_map: dict[int, dict] = {}
        try:
            result = subprocess.run(
                ["git", "blame", "--line-porcelain", rel_path],
                capture_output=True, text=True, timeout=30,
                cwd=str(repo_root),
            )
            if result.returncode != 0:
                logger.debug("git blame falhou para %s: %s", rel_path, result.stderr.strip())
                return blame_map

            # Parse do formato --line-porcelain:
            # Cada bloco começa com: <hash> <orig_lineno> <final_lineno> <num_lines>
            # Seguido por pares key value (ex: "author João", "committer-time 1234567890")
            current_line = None
            current_info: dict[str, str] = {}
            for raw_line in result.stdout.split("\n"):
                if raw_line.startswith("\t"):
                    # Linha de conteúdo (ignoramos)
                    if current_line is not None:
                        blame_map[current_line] = dict(current_info)
                    current_line = None
                    current_info = {}
                    continue

                if not raw_line.strip():
                    continue

                # Pode ser header (hash orig final nlines) ou key-value
                parts = raw_line.split()
                if not parts:
                    continue

                if len(parts) >= 4 and all(p.isdigit() for p in parts[1:4]) and len(parts[0]) == 40:
                    # Header: commit_hash orig_line final_line num_lines
                    current_line = int(parts[2])
                    current_info = {}
                elif " " in raw_line:
                    # key-value (primeiro espaço separa chave do valor)
                    idx = raw_line.index(" ")
                    key = raw_line[:idx]
                    value = raw_line[idx + 1:]
                    if current_line is not None:
                        current_info[key] = value

            # Último bloco (sem \t final? improvável mas seguro)
            if current_line is not None and current_info:
                blame_map[current_line] = dict(current_info)

        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("git blame exceção para %s: %s", rel_path, exc)

        return blame_map

    @staticmethod
    def _calc_age_days(committer_time: str | None) -> int | str:
        """Calcula idade em dias a partir do timestamp Unix do committer."""
        if committer_time is None:
            return "?"
        try:
            ts = int(committer_time)
            age_seconds = int(time.time()) - ts
            return max(0, age_seconds // 86400)
        except (ValueError, TypeError):
            return "?"

    def _build_table(self, markers: list[dict]) -> Table:
        """Constrói tabela Rich com os marcadores."""
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Arquivo", style="cyan", no_wrap=False, max_width=55)
        table.add_column("Linha", style="yellow", justify="right", width=6)
        table.add_column("Autor", style="green", width=18)
        table.add_column("Idade", style="magenta", justify="right", width=8)
        table.add_column("Marcador", style="bold red", width=8)

        # Ordena por arquivo, depois linha
        for m in sorted(markers, key=lambda x: (x["file"], x["line"])):
            age_str = f"{m['age_days']}d" if isinstance(m["age_days"], int) else "?"
            table.add_row(
                str(m["file"]),
                str(m["line"]),
                m["author"],
                age_str,
                m["marker"],
            )

        return table
