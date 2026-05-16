"""RewindCommand — /rewind: go back to an earlier point in the conversation."""

from __future__ import annotations

import time
import uuid

from rich.panel import Panel
from rich.text import Text

from ...core.interfaces.selector import SelectorNotSupported, SelectorOption
from ...infrastructure.selectors import get_default_selector
from .._sentinels import SWITCH_SESSION_KEY
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import truncate_oneline, wrap_command_errors

_MAX_LABEL = 80


class RewindCommand(DirectCommand):
    """Go back to an earlier point in the conversation.

    Shows the current conversation history as a navigable list.  Selecting a
    message creates a **fork** of the conversation up to and including that
    point, then switches to it — the original thread is not modified.

    ESC cancels with no effect.
    """

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="rewind",
                description="Navigate history and fork at a chosen point.",
                aliases=["rw"],
            )
        )

    @wrap_command_errors("rewind")
    async def execute(self, context: CommandContext) -> CommandResult:
        agent = context.agent
        session = context.session
        if agent is None or session is None:
            return CommandResult.error_result("Agent or session not available.")

        history = list(getattr(session, "conversation_history", []))
        user_entries = [
            (i, e) for i, e in enumerate(history) if e.get("role") == "user"
        ]

        if not user_entries:
            return CommandResult(
                success=True,
                content=Panel(
                    Text("Nenhuma mensagem no histórico desta conversa.", style="dim"),
                    title="Rewind",
                    border_style="yellow",
                ),
            )

        selector = get_default_selector()
        if not selector.is_supported():
            lines = "\n".join(
                f"{idx + 1}. {truncate_oneline(e['content'], _MAX_LABEL)}"
                for idx, (_, e) in enumerate(user_entries)
            )
            return CommandResult(
                success=False,
                content=Panel(
                    Text(
                        f"Seletor interativo não disponível (sem TTY).\n\n{lines}",
                        style="yellow",
                    ),
                    title="Rewind — sem TTY",
                    border_style="yellow",
                ),
            )

        options = [
            SelectorOption(
                label=f"#{idx + 1}  {truncate_oneline(e['content'], _MAX_LABEL)}",
                value=hist_idx,
                description="",
            )
            for idx, (hist_idx, e) in enumerate(user_entries)
        ]

        try:
            choice = await selector.select(
                options,
                prompt="Rewind — selecione o ponto de retorno (↑↓ Enter ESC):",
                default_index=len(options) - 1,
            )
        except SelectorNotSupported:
            return CommandResult.error_result("Seletor não suportado neste ambiente.")

        if choice is None:
            return CommandResult(
                success=True,
                content=Panel(
                    Text("Rewind cancelado.", style="dim"),
                    title="Rewind",
                    border_style="yellow",
                ),
                metadata={"cancelled": True},
            )

        cut = int(choice.value) + 1
        trimmed = history[:cut]

        new_sid = f"rewind-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        try:
            new_session = agent.create_session(
                session_id=new_sid,
                working_directory=session.working_directory,
            )
        except Exception as exc:
            return CommandResult.error_result(f"Não foi possível criar sessão: {exc}")

        new_session.conversation_history = [dict(e) for e in trimmed]

        orig_name = session.context_data.get("conversation_name", "")
        if orig_name:
            new_session.context_data["conversation_name"] = f"{orig_name} (rewind)"

        session.context_data[SWITCH_SESSION_KEY] = new_sid

        label = truncate_oneline(trimmed[-1]["content"], _MAX_LABEL) if trimmed else "—"
        return CommandResult(
            success=True,
            content=Panel(
                Text.from_markup(
                    f"Fork criado a partir de: [bold cyan]{label}[/bold cyan]\n"
                    f"[dim]{len(trimmed)} mensagem(ns) preservada(s) · ID: {new_sid}[/dim]"
                ),
                title="[bold green]Rewind — fork criado[/bold green]",
                border_style="green",
            ),
        )
