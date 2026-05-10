"""Comando /version — exibe versão, build, métricas e ambiente do DEILE (issue #173).

Mapeia para:
  • o comando slash ``/version`` no modo interativo, e
  • a flag CLI ``--version`` (gerada automaticamente via metadado cli_flag).
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import platform
import sys
import time

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import FLAG_DESCRICOES_PTBR as _FLAG_DESCRICOES

logger = logging.getLogger(__name__)

_LINKS = {
    "Repositório": "https://github.com/elimarcavalli/deile",
    "Documentação": "docs/system_design/00-VISAO-GERAL.md",
    "Licença": "MIT — https://opensource.org/licenses/MIT",
    "Issues": "https://github.com/elimarcavalli/deile/issues",
}


def _detect_install_info() -> dict[str, str]:
    """Detecta modo de instalação via importlib.metadata (sem subprocess)."""
    try:
        dist = importlib.metadata.distribution("deile")
        version = dist.metadata["Version"] or "desconhecida"
        location = str(dist.locate_file("")).rstrip("/")

        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            editable = direct_url.get("dir_info", {}).get("editable", False)
            modo = "desenvolvimento (editable install)" if editable else "instalação normal"
        else:
            modo = "instalação normal"

        return {"modo": modo, "versao_pkg": version, "diretorio": location}
    except Exception as exc:
        logger.debug("Não foi possível detectar info de instalação: %s", exc)
        return {"modo": "indisponível", "versao_pkg": "indisponível", "diretorio": "indisponível"}


def _build_info_table(rows: list[tuple[str, str]]) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    for label, value in rows:
        grid.add_row(label, value)
    return grid


class VersionCommand(DirectCommand):
    """``/version`` — exibe versão, build, métricas e ambiente do DEILE."""

    cli_flag = "--version"
    cli_help = "Exibe a versão do DEILE e sai."
    cli_requires_provider = False

    def __init__(self) -> None:
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="version",
            description="Exibe versão, build, métricas de código e ambiente.",
            aliases=["ver"],
        )
        super().__init__(config)
        self.category = "system"

    async def execute(self, context: CommandContext) -> CommandResult:
        """Renderiza informações de versão enriquecidas em PTBR."""
        t_start = time.monotonic()

        try:
            from deile.__version__ import (FEATURES, METRICS, __build_date__,
                                           __build_number__, __description__,
                                           __license__, __title__, __version__)
        except ImportError as exc:  # pragma: no cover — pacote sempre presente
            logger.error("Falha ao importar deile.__version__: %s", exc)
            return CommandResult.error_result(
                f"Não foi possível determinar a versão do DEILE: {exc}",
                error=exc,
            )

        install_info = _detect_install_info()
        py_impl = platform.python_implementation()
        py_ver = sys.version.split()[0]
        plat_str = platform.platform()

        # --- Seção: Informações gerais ---
        info_rows = [
            ("Versão", f"[bold cyan]{__title__}[/bold cyan] [bold]v{__version__}[/bold]"),
            ("Descrição", __description__),
            ("Build", f"{__build_number__}  •  {__build_date__}"),
            ("Licença", __license__),
        ]
        info_table = _build_info_table(info_rows)

        # --- Seção: Ambiente ---
        env_rows = [
            ("Python", f"{py_ver} ({py_impl})"),
            ("Plataforma", plat_str),
        ]
        env_table = _build_info_table(env_rows)

        # --- Seção: Instalação ---
        install_rows = [
            ("Modo", install_info["modo"]),
            ("Pacote", f"deile {install_info['versao_pkg']}"),
            ("Diretório", install_info["diretorio"]),
        ]
        install_table = _build_info_table(install_rows)

        # --- Seção: Métricas ---
        try:
            metrics_rows = [
                ("Total de arquivos", str(METRICS.get("total_files", "—"))),
                ("Total de linhas", str(METRICS.get("total_lines", "—"))),
                ("Comandos", str(METRICS.get("commands", "—"))),
                ("Tools", str(METRICS.get("tools", "—"))),
                ("Arquivos de teste", str(METRICS.get("test_files", "—"))),
                ("Cobertura", str(METRICS.get("coverage", "—"))),
            ]
        except Exception as exc:
            logger.warning("Métricas indisponíveis: %s", exc)
            metrics_rows = [("Métricas", "indisponível")]
        metrics_table = _build_info_table(metrics_rows)

        # --- Seção: Feature flags ativas ---
        active_flags = [k for k, v in FEATURES.items() if v]
        flag_lines = [
            f"  [green]✅[/green] [bold]{flag}[/bold]  —  {_FLAG_DESCRICOES.get(flag, '—')}"
            for flag in active_flags
        ]
        flags_text = Text.from_markup(
            "\n".join(flag_lines) if flag_lines else "  (nenhuma flag ativa)"
        )

        # --- Seção: Links ---
        link_lines = [f"  [dim]{k}:[/dim]  {v}" for k, v in _LINKS.items()]
        links_text = Text.from_markup("\n".join(link_lines))

        content = Group(
            Text.from_markup("[bold]Informações gerais[/bold]"),
            info_table,
            Text(""),
            Text.from_markup("[bold]Ambiente[/bold]"),
            env_table,
            Text(""),
            Text.from_markup("[bold]Instalação[/bold]"),
            install_table,
            Text(""),
            Text.from_markup("[bold]Métricas de código[/bold]"),
            metrics_table,
            Text(""),
            Text.from_markup("[bold]Feature flags ativas[/bold]"),
            flags_text,
            Text(""),
            Text.from_markup("[bold]Links[/bold]"),
            links_text,
        )

        panel = Panel(
            content,
            title="[bold cyan]DEILE Version[/bold cyan]",
            border_style="cyan",
        )

        elapsed = time.monotonic() - t_start
        logger.debug("/version renderizado em %.3fs", elapsed)

        return CommandResult.success_result(
            panel,
            "rich",
            version=__version__,
            build_date=__build_date__,
            build_number=__build_number__,
            elapsed_s=round(elapsed, 3),
        )
