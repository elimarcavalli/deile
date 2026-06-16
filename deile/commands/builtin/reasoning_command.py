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
from ...core.models.reasoning import (
    CLAUDE_CODE_EFFORTS,
    DEEPSEEK_EFFORTS,
    GEMINI_EFFORTS,
    OPENAI_EFFORTS,
    is_valid_effort,
    normalize_effort,
)
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
        super().__init__(
            CommandConfig(
                name="reasoning",
                description="Set the reasoning effort for this session (low|medium|high|xhigh|max|...)",
                aliases=["effort"],
            )
        )
        self.category = "ai"
        self.help_text = """
Reasoning Command — esforço de raciocínio do turno

USAGE:
    /reasoning                 Mostra o esforço atual e os níveis válidos
    /reasoning <nível>         Fixa o esforço SOFT para esta sessão
    /reasoning clear           Remove o override SOFT (volta ao global)
    /reasoning use <nível>     Hard override — vence injeção per-turn do worker
    /reasoning use clear       Remove o hard override (idempotente)

EIXOS:
    SOFT (/reasoning <nível>)  — grava session.context_data["reasoning_effort"]
    HARD (/reasoning use <n>)  — grava session.context_data["forced_reasoning_effort"],
                                  lido antes do SOFT em resolve_session_reasoning

NÍVEIS:
    anthropic / claude-worker: low | medium | high | xhigh | max | ultracode | auto
    openai:   none | minimal | low | medium | high | xhigh | auto
    gemini:   off | minimal | low | medium | high | auto
    deepseek: off | high | max | auto

    /reasoning use auto  → armazena "auto" (provider-default forçado, NÃO é clear)
    /reasoning use clear → limpa o hard override (_CLEAR_KEYWORDS: clear/reset/...)

EXEMPLOS:
    /reasoning high
    /reasoning use ultracode
    /reasoning use auto
    /reasoning use clear
    /reasoning clear
"""

    async def execute(self, context: CommandContext) -> CommandResult:
        args = split_args(context)
        if not args or args[0].lower() in ("show", "status"):
            return self._show(context)
        if args[0].lower() == "use":
            target = args[1].strip().lower() if len(args) > 1 else ""
            return self._use(target, context)
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
        """Retorna ``(valor, fonte)`` do esforço efetivo: forced > sessão > global > default."""
        cd = self._ctx_data(context) or {}
        forced = normalize_effort(cd.get("forced_reasoning_effort"))
        if forced:
            return forced, "forced (hard)"
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

    def _use(self, target: str, context: CommandContext) -> CommandResult:
        """Despacha ``/reasoning use <nível|clear>`` — eixo HARD."""
        if not target:
            return CommandResult(
                success=False,
                content=Panel(
                    Text(
                        "Usage: /reasoning use <nível>  ou  /reasoning use clear",
                        style="yellow",
                    ),
                    title="Reasoning effort",
                    border_style="yellow",
                ),
            )
        if target in _CLEAR_KEYWORDS:
            return self._use_clear(context)
        return self._use_set(target, context)

    def _use_set(self, target: str, context: CommandContext) -> CommandResult:
        """Grava ``forced_reasoning_effort`` (hard override)."""
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
                content=Panel(
                    Text("Sessão indisponível.", style="red"),
                    title="Error",
                    border_style="red",
                ),
            )
        cd["forced_reasoning_effort"] = target
        logger.info("forced_reasoning_effort setado em '%s'", target)
        return CommandResult(
            success=True,
            content=Panel(
                Text(
                    f"Hard override: esforço fixado em '{target}' (vence injeção per-turn do worker)."
                ),
                title="Reasoning effort — hard override",
                border_style="green",
            ),
            metadata={"forced_reasoning_effort": target},
        )

    def _use_clear(self, context: CommandContext) -> CommandResult:
        """Remove ``forced_reasoning_effort`` (idempotente)."""
        cd = self._ctx_data(context)
        if cd is not None:
            cd.pop("forced_reasoning_effort", None)
        logger.info("forced_reasoning_effort limpo")
        return CommandResult(
            success=True,
            content=Panel(
                Text(
                    "Hard override removido — esforço volta ao slot SOFT / global / default."
                ),
                title="Reasoning effort — hard override",
                border_style="green",
            ),
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
                content=Panel(
                    Text("Sessão indisponível.", style="red"),
                    title="Error",
                    border_style="red",
                ),
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
