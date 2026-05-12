"""RenameCommand — /rename: give a human-readable name to the current conversation."""

from __future__ import annotations

from rich.panel import Panel
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._conv_store import ConversationNameStore
from ._shared import split_args, wrap_command_errors


class RenameCommand(DirectCommand):
    """Rename the current conversation session.

    The name is persisted in ``~/.deile/conversation_names.json`` and shown
    by ``/resume`` when listing past conversations.
    """

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="rename",
                description="Name the current conversation for easier resumption.",
            )
        )

    @wrap_command_errors("rename")
    async def execute(self, context: CommandContext) -> CommandResult:
        session = context.session
        if session is None:
            return CommandResult.error_result("No active session.")

        parts = split_args(context)
        name = " ".join(parts).strip() if parts else ""
        if not name:
            return CommandResult.error_result(
                "Uso: /rename <nome>\nEx: /rename minha-conversa-de-debug"
            )

        store = ConversationNameStore()
        store.set(session.session_id, name)
        session.context_data["conversation_name"] = name

        return CommandResult(
            success=True,
            content=Panel(
                Text.from_markup(
                    f"Conversa renomeada para [bold cyan]{name}[/bold cyan].\n"
                    f"[dim]ID: {session.session_id}[/dim]"
                ),
                title="[bold green]Conversa renomeada[/bold green]",
                border_style="green",
            ),
        )
