"""ForkCommand — /fork: branch the current conversation into a new session."""

from __future__ import annotations

import time
import uuid

from rich.panel import Panel
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._conv_store import ConversationNameStore
from ._shared import split_args, wrap_command_errors

_SENTINEL = "_switch_session"


class ForkCommand(DirectCommand):
    """Fork the current conversation into a new independent session.

    The fork starts with a copy of the current conversation history so the
    dialogue can continue in parallel without affecting the original thread.
    Optionally assign a name to the fork with ``/fork <name>``.
    """

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="fork",
                description="Fork the current conversation into a new named session.",
            )
        )

    @wrap_command_errors("fork")
    async def execute(self, context: CommandContext) -> CommandResult:
        agent = context.agent
        session = context.session
        if agent is None or session is None:
            return CommandResult.error_result("Agent or session not available.")

        parts = split_args(context)
        name: str = " ".join(parts).strip() if parts else ""

        history = list(getattr(session, "conversation_history", []))
        new_sid = f"fork-{int(time.time())}-{uuid.uuid4().hex[:8]}"

        try:
            new_session = agent.create_session(
                session_id=new_sid,
                working_directory=session.working_directory,
            )
        except Exception as exc:
            return CommandResult.error_result(f"Could not create forked session: {exc}")

        new_session.conversation_history = [dict(e) for e in history]

        if name:
            store = ConversationNameStore()
            store.set(new_sid, name)
            new_session.context_data["conversation_name"] = name

        session.context_data[_SENTINEL] = new_sid

        label = f'"{name}"' if name else new_sid
        return CommandResult(
            success=True,
            content=Panel(
                Text.from_markup(
                    f"Conversa forked para [bold cyan]{label}[/bold cyan].\n"
                    f"[dim]ID: {new_sid}[/dim]\n"
                    f"[dim]{len(history)} mensagem(ns) copiada(s)[/dim]"
                ),
                title="[bold green]Fork criado[/bold green]",
                border_style="green",
            ),
        )
