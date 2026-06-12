"""Comando /loc — exibe estatísticas do código-base (issue #285)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._git_helpers import git_ls_files

logger = logging.getLogger(__name__)

class LocCommand(DirectCommand):
    """``/loc`` — exibe estatísticas do código-base."""

    cli_flag = "--loc"
    cli_help = "Exibe estatísticas do código-base e sai."
    cli_requires_provider = False

    def __init__(self) -> None:
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="loc",
            description="Exibe estatísticas do código-base (linhas, arquivos, testes).",
            aliases=["estatisticas"],
        )
        super().__init__(config)
        self.category = "system"

    def _get_git_files(self, cwd: str) -> list[str]:
        try:
            return git_ls_files(cwd)
        except CommandError as exc:
            logger.warning("Falha ao executar git ls-files: %s", exc)
            return []

    def _count_lines(self, filepath: str) -> int:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
        except Exception as exc:  # open por arquivo é best-effort — 0 linhas em falha
            logger.debug("loc: falha ao contar linhas em %s: %s", filepath, exc)
            return 0

    _LANGUAGE_BY_EXT: dict[str, str] = {
        ".py": "Python",
        ".md": "Markdown",
        ".yaml": "YAML",
        ".yml": "YAML",
        ".json": "JSON",
        ".sh": "Shell",
    }

    def _get_language(self, filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower()
        return self._LANGUAGE_BY_EXT.get(ext, "Other")

    def _count_tests(self, cwd: str) -> int:
        test_dir = Path(cwd) / "deile" / "tests"
        if not test_dir.exists():
            return 0
        
        count = 0
        for root, _, files in os.walk(test_dir):
            for file in files:
                if file.endswith(".py"):
                    filepath = os.path.join(root, file)
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                stripped = line.strip()
                                if stripped.startswith("def test_") or stripped.startswith("async def test_"):
                                    count += 1
                    except Exception as exc:  # open por arquivo é best-effort — ignora arquivo ilegível
                        logger.debug("loc: falha ao contar testes em %s: %s", filepath, exc)
        return count

    def _collect_stats(self, cwd: str) -> dict:
        """Coleta estatísticas do código-base — I/O bloqueante isolado.

        Executa ``git ls-files``, abre cada arquivo versionado para contar
        linhas e varre ``deile/tests`` com ``os.walk`` + ``open`` para contar
        funções de teste. Todo o trabalho é síncrono e bloqueante; o
        ``execute()`` despacha esta função via ``asyncio.to_thread`` para
        não travar o event loop (pilar 03 §1).
        """
        files = self._get_git_files(cwd)

        lang_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "lines": 0})
        file_sizes: list[tuple[str, int]] = []

        total_files = 0
        total_lines = 0

        for file in files:
            filepath = os.path.join(cwd, file)
            if not os.path.isfile(filepath):
                continue

            lines = self._count_lines(filepath)
            lang = self._get_language(file)

            lang_stats[lang]["files"] += 1
            lang_stats[lang]["lines"] += lines

            file_sizes.append((file, lines))

            total_files += 1
            total_lines += lines

        total_tests = self._count_tests(cwd)

        return {
            "lang_stats": lang_stats,
            "file_sizes": file_sizes,
            "total_files": total_files,
            "total_lines": total_lines,
            "total_tests": total_tests,
        }

    async def execute(self, context: CommandContext) -> CommandResult:
        """Renderiza estatísticas do código-base."""
        cwd = context.working_directory or os.getcwd()

        # Toda coleta (git ls-files + open por arquivo + os.walk +
        # open por arquivo de teste) é I/O bloqueante; off-load para
        # uma worker thread mantém o event loop responsivo
        # (pilar 03 §1, alinhado com /todo).
        stats = await asyncio.to_thread(self._collect_stats, cwd)
        lang_stats = stats["lang_stats"]
        file_sizes = stats["file_sizes"]
        total_files = stats["total_files"]
        total_lines = stats["total_lines"]
        total_tests = stats["total_tests"]

        # Ordenar linguagens por número de linhas (decrescente)
        sorted_langs = sorted(lang_stats.items(), key=lambda x: x[1]["lines"], reverse=True)

        # Top 5 maiores arquivos
        top_files = sorted(file_sizes, key=lambda x: x[1], reverse=True)[:5]

        # --- Tabela de Linguagens ---
        table = Table(title="Linhas por linguagem", title_justify="left")
        table.add_column("Linguagem", style="cyan")
        table.add_column("Arquivos", justify="right", style="green")
        table.add_column("Linhas", justify="right", style="yellow")
        
        for lang, lang_row in sorted_langs:
            table.add_row(lang, str(lang_row["files"]), str(lang_row["lines"]))
            
        # --- Top 5 Arquivos ---
        top_files_text = Text()
        top_files_text.append("Top 5 maiores arquivos\n", style="bold")
        for file, lines in top_files:
            top_files_text.append(f"- {file} — {lines} linhas\n")
            
        # --- Resumo ---
        summary_text = Text(f"\nTotal: {total_files} arquivos · {total_lines} linhas · {total_tests} testes pytest", style="bold")
        
        content = Group(
            table,
            Text(""),
            top_files_text,
            summary_text
        )
        
        panel = Panel(
            content,
            title="[bold]📊 DEILE — Estatísticas do código-base[/bold]",
            border_style="blue",
        )
        
        return CommandResult.success_result(
            panel,
            "rich",
            total_files=total_files,
            total_lines=total_lines,
            total_tests=total_tests,
            lang_stats=dict(lang_stats),
            top_files=top_files
        )
