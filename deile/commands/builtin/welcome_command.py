"""Comando /welcome — mensagem de boas-vindas e guia de início rápido (issue #174).

Exibe dados dinâmicos reais: versão de __version__, modelo ativo via contexto,
quick start gerada via CommandRegistry, e feature list baseada em FEATURES reais.
"""

from __future__ import annotations

import logging

from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import FLAG_DESCRICOES_PTBR as _FLAG_DESCRICOES_PTBR

logger = logging.getLogger(__name__)

# Quick start candidates — verificados contra CommandRegistry em runtime.
# Ordem define a prioridade de exibição.
_QUICK_START_CANDIDATES = [
    {"nome": "help", "acao": "Listar todos os comandos", "descricao": "Ajuda completa"},
    {"nome": "status", "acao": "Checar status do sistema", "descricao": "Visão geral do DEILE"},
    {"nome": "version", "acao": "Exibir versão e build", "descricao": "Informações de versão"},
    {"nome": "plan", "acao": "Cria e executa fluxos autônomos", "descricao": "Iniciar fluxo autônomo"},
    {"nome": "memory", "acao": "Ver memória da sessão", "descricao": "Estado da memória"},
    {"nome": "permissions", "acao": "Gerenciar permissões", "descricao": "Configuração de segurança"},
    {"nome": "cost", "acao": "Ver custos e tokens", "descricao": "Uso e custo estimado"},
    {"nome": "context", "acao": "Ver contexto atual", "descricao": "Contexto da sessão"},
]

_LINKS = {
    "Repositório": "https://github.com/elimarcavalli/deile",
    "Documentação": "docs/system_design/00-VISAO-GERAL.md",
    "Licença": "MIT",
    "Issues": "https://github.com/elimarcavalli/deile/issues",
}

_WORKFLOWS = [
    ("Análise e refatoração", "/find 'TODO|FIXME' → /plan create → /run"),
    ("Fluxo de desenvolvimento", "/bash 'git status' → /plan create 'Deploy' → /approve"),
    ("Segurança e monitoramento", "/permissions → /logs security → /sandbox on"),
    ("Gestão de sessão", "/memory status → /export → /cls reset"),
]

_DICAS = [
    "Use '/help <comando>' para ajuda detalhada de um comando específico.",
    "Digite '/' para ver todos os comandos disponíveis.",
    "Use '@' para autocompletar caminhos de arquivo em comandos.",
    "Operações de alto risco requerem aprovação manual (/approve).",
    "Salve seu trabalho com /memory save antes de alterações importantes.",
    "Use '/cls reset' para um início limpo se necessário.",
]


def _get_version() -> str:
    try:
        from deile.__version__ import __version__
        return __version__
    except Exception as exc:
        logger.debug("Não foi possível obter versão: %s", exc)
        return "—"


def _get_active_features() -> list[str]:
    try:
        from deile.__version__ import FEATURES
        return [k for k, v in FEATURES.items() if v]
    except Exception as exc:
        logger.debug("Não foi possível obter features: %s", exc)
        return []


def _get_active_model(context: CommandContext) -> str:
    try:
        agent = context.agent
        if agent is not None:
            router = getattr(agent, "model_router", None) or getattr(agent, "tier_router", None)
            if router is not None:
                model = getattr(router, "current_model", None) or getattr(router, "default_model", None)
                if model:
                    return str(model)
        return "—"
    except Exception as exc:
        logger.debug("Não foi possível obter modelo ativo: %s", exc)
        return "—"


def _get_quick_start_verified(context: CommandContext) -> list[dict[str, str]]:
    """Retorna candidates filtrados pelos que existem no CommandRegistry."""
    try:
        from deile.commands.registry import get_command_registry
        registry = get_command_registry(context.config_manager)
        registered = {cmd.name for cmd in registry.get_all_commands()}
        return [c for c in _QUICK_START_CANDIDATES if c["nome"] in registered]
    except Exception as exc:
        logger.warning("Não foi possível verificar CommandRegistry: %s", exc)
        return []


class WelcomeCommand(DirectCommand):
    """``/welcome`` — boas-vindas e guia de início rápido do DEILE."""

    def __init__(self) -> None:
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="welcome",
            description="Exibe mensagem de boas-vindas e guia de início rápido.",
        )
        super().__init__(config)

    async def execute(self, context: CommandContext) -> CommandResult:
        """Renderiza o guia de boas-vindas com dados dinâmicos reais."""
        version = _get_version()
        active_model = _get_active_model(context)
        active_features = _get_active_features()
        quick_start = _get_quick_start_verified(context)

        # --- Cabeçalho ---
        header = Text()
        header.append("Bem-vindo ao ", style="white")
        header.append("DEILE ", style="bold cyan")
        header.append(f"v{version}", style="bold bright_green")
        header.append("\n\n")
        header.append(
            "Seu assistente autônomo de desenvolvimento com execução inteligente.",
            style="dim",
        )
        if active_model != "—":
            header.append("\n\nModelo ativo: ", style="dim")
            header.append(active_model)

        header_panel = Panel(
            header,
            title="[bold bright_blue]DEILE — Development Environment Intelligence & Learning Engine[/bold bright_blue]",
            border_style="bright_blue",
            padding=(1, 2),
        )

        # --- Início rápido ---
        qs_table = Table(
            title="Início Rápido",
            show_header=True,
            header_style="bold yellow",
        )
        qs_table.add_column("Ação", style="cyan", width=25)
        qs_table.add_column("Comando", style="green", width=22)
        qs_table.add_column("Descrição", style="white", width=30)
        for entry in quick_start:
            qs_table.add_row(entry["descricao"], f"/{entry['nome']}", entry["acao"])

        # --- Features ativas ---
        features_text = Text()
        for flag in active_features:
            desc = _FLAG_DESCRICOES_PTBR.get(flag, flag)
            features_text.append("✅ ", style="green")
            features_text.append(f"{desc}\n")
        if not active_features:
            features_text.append("(nenhuma feature ativa)", style="dim")

        features_panel = Panel(
            features_text,
            title="Capacidades Ativas",
            border_style="yellow",
        )

        # --- Fluxos comuns ---
        workflows_text = Text()
        for i, (nome, fluxo) in enumerate(_WORKFLOWS, 1):
            workflows_text.append(f"{i}. ", style="bright_blue")
            workflows_text.append(f"{nome}\n", style="bold")
            workflows_text.append(f"   {fluxo}\n\n", style="dim")

        workflows_panel = Panel(
            workflows_text,
            title="Fluxos Comuns",
            border_style="green",
        )

        # --- Dicas ---
        dicas_text = Text()
        for dica in _DICAS:
            dicas_text.append("• ", style="yellow")
            dicas_text.append(f"{dica}\n", style="dim")

        dicas_panel = Panel(
            dicas_text,
            title="Dicas",
            border_style="magenta",
        )

        # --- Ajuda e links ---
        ajuda_text = Text()
        ajuda_text.append("📚 ", style="blue")
        ajuda_text.append("Documentação: ", style="bold")
        ajuda_text.append(f"{_LINKS['Documentação']}\n", style="dim")
        ajuda_text.append("🔧 ", style="green")
        ajuda_text.append("Modo debug: ", style="bold")
        ajuda_text.append("/debug on (ativa log detalhado)\n", style="dim")
        ajuda_text.append("📊 ", style="yellow")
        ajuda_text.append("Info do sistema: ", style="bold")
        ajuda_text.append("/status (versão, conectividade, tools)\n", style="dim")
        ajuda_text.append("💰 ", style="magenta")
        ajuda_text.append("Rastreamento de uso: ", style="bold")
        ajuda_text.append("/cost (tokens e custo estimado)\n", style="dim")
        ajuda_text.append("🐛 ", style="red")
        ajuda_text.append("Issues: ", style="bold")
        ajuda_text.append(_LINKS["Issues"], style="dim")

        ajuda_panel = Panel(
            ajuda_text,
            title="Ajuda e Suporte",
            border_style="cyan",
        )

        content = Group(
            header_panel,
            "",
            qs_table,
            "",
            Columns([features_panel, workflows_panel]),
            "",
            Columns([dicas_panel, ajuda_panel]),
        )

        return CommandResult.success_result(content, "rich")
