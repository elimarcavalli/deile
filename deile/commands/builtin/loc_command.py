"""Comando /loc — exibe estatísticas do código-base (issue #285)."""

from __future__ import annotations

import logging
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand

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
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True
            )
            return [f for f in result.stdout.splitlines() if f.strip()]
        except subprocess.CalledProcessError as exc:
            logger.warning("Falha ao executar git ls-files: %s", exc)
            return []
        except FileNotFoundError:
            logger.warning("Git não encontrado no sistema.")
            return []

    def _count_lines(self, filepath: str) -> int:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _get_language(self, filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower()
        if ext == ".py":
            return "Python"
        elif ext == ".md":
            return "Markdown"
        elif ext in (".yaml", ".yml"):
            return "YAML"
        elif ext == ".json":
            return "JSON"
        elif ext == ".sh":
            return "Shell"
        else:
            return "Other"

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
                    except Exception:
                        pass
        return count

    async def execute(self, context: CommandContext) -> CommandResult:
        """Renderiza estatísticas do código-base."""
        cwd = context.working_directory or os.getcwd()
        
        files = self._get_git_files(cwd)
        
        lang_stats = defaultdict(lambda: {"files": 0, "lines": 0})
        file_sizes = []
        
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
            
        # Ordenar linguagens por número de linhas (decrescente)
        sorted_langs = sorted(lang_stats.items(), key=lambda x: x[1]["lines"], reverse=True)
        
        # Top 5 maiores arquivos
        top_files = sorted(file_sizes, key=lambda x: x[1], reverse=True)[:5]
        
        # Total de testes
        total_tests = self._count_tests(cwd)
        
        # --- Tabela de Linguagens ---
        table = Table(title="Linhas por linguagem", title_justify="left")
        table.add_column("Linguagem", style="cyan")
        table.add_column("Arquivos", justify="right", style="green")
        table.add_column("Linhas", justify="right", style="yellow")
        
        for lang, stats in sorted_langs:
            table.add_row(lang, str(stats["files"]), str(stats["lines"]))
            
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
