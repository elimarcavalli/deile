"""ReasoningCommand — configura o esforço de raciocínio do turno (DEILE CLI).

Espelha ``ModelCommand`` no eixo do *reasoning effort*: ``/reasoning <nível>``
fixa ``session.context_data["reasoning_effort"]`` para a sessão (soft override,
lido por ``resolve_session_reasoning`` em ``deile/core/models/reasoning.py`` e
traduzido por cada provider). ``/reasoning clear`` remove o override (volta ao
global ``settings.reasoning_effort``). O flag CLI one-shot equivalente é
``--reasoning LEVEL`` (ver ``deile/cli.py``).
"""

from __future__ import annotations

import logging
from typing import Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...config.manager import CommandConfig
from ...config.settings import get_settings
from ...core.models.reasoning import (CLAUDE_CODE_EFFORTS, DEEPSEEK_EFFORTS,
                                      GEMINI_EFFORTS, OPENAI_EFFORTS,
                                      is_valid_effort, normalize_effort)
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import split_args

logger = logging.getLogger(__name__)

_CLEAR_KEYWORDS = {"clear", "reset", "default", "none-override", "unset"}


class ReasoningCommand(DirectCommand):
    """Gerencia o esforço de raciocínio do turno (low|medium|high|...)."""

    # O flag one-shot ``--reasoning`` é declarado diretamente em ``cli.py``
    # (paridade com ``--model``), então o comando não declara ``cli_flag``.
    cli_flag = None
    cli_requires_provider = False

    def __init__(self) -> None:
        super().__init__(CommandConfig(
            name="reasoning",
            description="Set the reasoning effort for this session (low|medium|high|xhigh|max|...)",
            aliases=["effort"],
        ))
        self.category = "ai"
        self.help_text = """
Reasoning Command — esforço de raciocínio do turno

USAGE:
    /reasoning                 Mostra o esforço atual e os níveis válidos
    /reasoning <nível>         Fixa o esforço para esta sessão
    /reasoning clear           Remove o override (volta ao global)

NÍVEIS:
    anthropic / claude-worker: low | medium | high | xhigh | max | ultracode | auto
    openai:   none | minimal | low | medium | high | xhigh | auto
    gemini:   off | minimal | low | medium | high | auto
    deepseek: off | high | max | auto

EXEMPLOS:
    /reasoning high
    /reasoning ultracode
    /reasoning clear
"""

    async def execute(self, context: CommandContext) -> CommandResult:
        args = split_args(context)
        if not args or args[0].lower() in ("show", "status"):
            return self._show(context)
        target = args[0].strip().lower()
        if target in _CLEAR_KEYWORDS:
            return self._clear(context)
        return self._set(target, context)

    # ------------------------------------------------------------------

    @staticmethod
    def _ctx_data(context: CommandContext) -> Optional[dict]:
        """Garante e retorna o ``context_data`` da sessão, ou ``None`` se ausente."""
        session = getattr(context, "session", None)
        if session is None:
            return None
        if getattr(session, "context_data", None) is None:
            try:
                session.context_data = {}
            except (AttributeError, TypeError):
                return None
        return session.context_data

    def _current_effort(self, context: CommandContext) -> tuple:
        """Retorna ``(valor, fonte)`` do esforço efetivo: sessão > global > default."""
        cd = self._ctx_data(context) or {}
        v = normalize_effort(cd.get("reasoning_effort"))
        if v:
            return v, "session (/reasoning)"
        try:
            g = normalize_effort(get_settings().reasoning_effort)
        except Exception:  # noqa: BLE001
            g = None
        if g:
            return g, "global (settings/env)"
        return None, "provider default"

    def _show(self, context: CommandContext) -> CommandResult:
        current, source = self._current_effort(context)
        tbl = Table(show_header=True, header_style="bold cyan", expand=True)
        tbl.add_column("provider / worker")
        tbl.add_column("níveis válidos")
        tbl.add_row("anthropic · claude-worker", " | ".join(CLAUDE_CODE_EFFORTS))
        tbl.add_row("openai", " | ".join(OPENAI_EFFORTS))
        tbl.add_row("gemini", " | ".join(GEMINI_EFFORTS))
        tbl.add_row("deepseek", " | ".join(DEEPSEEK_EFFORTS))
        body = Text()
        body.append("Esforço atual: ", style="bold")
        body.append(f"{current or '(default do provider)'}", style="bold green")
        body.append(f"   ·   fonte: {source}\n\n", style="dim")
        return CommandResult(
            success=True,
            content=Panel(
                body,
                title="Reasoning effort",
                subtitle="/reasoning <nível>  ·  /reasoning clear",
                border_style="cyan",
            ),
            metadata={"reasoning_effort": current, "source": source},
        )

    def _set(self, target: str, context: CommandContext) -> CommandResult:
        if not is_valid_effort(target):
            return CommandResult(
                success=False,
                content=Panel(
                    Text(
                        f"Nível inválido '{target}'.\n"
                        "Anthropic/claude: " + " | ".join(CLAUDE_CODE_EFFORTS) + "\n"
                        "openai: " + " | ".join(OPENAI_EFFORTS) + "\n"
                        "gemini: " + " | ".join(GEMINI_EFFORTS) + "\n"
                        "deepseek: " + " | ".join(DEEPSEEK_EFFORTS),
                        style="yellow",
                    ),
                    title="[bold red]Reasoning effort inválido[/bold red]",
                    border_style="red",
                ),
            )
        cd = self._ctx_data(context)
        if cd is None:
            return CommandResult(
                success=False,
                content=Panel(Text("Sessão indisponível.", style="red"),
                              title="Error", border_style="red"),
            )
        cd["reasoning_effort"] = target
        return CommandResult(
            success=True,
            content=Panel(
                Text(f"Esforço de raciocínio fixado em '{target}' para esta sessão."),
                title="Reasoning effort",
                border_style="green",
            ),
            metadata={"reasoning_effort": target},
        )

    def _clear(self, context: CommandContext) -> CommandResult:
        cd = self._ctx_data(context)
        if cd is not None:
            cd.pop("reasoning_effort", None)
        return CommandResult(
            success=True,
            content=Panel(
                Text("Override removido — voltando ao esforço global / default."),
                title="Reasoning effort",
                border_style="green",
            ),
        )
