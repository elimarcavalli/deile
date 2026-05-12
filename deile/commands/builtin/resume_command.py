"""ResumeCommand — /resume: list past conversations and reload one."""

from __future__ import annotations

import time
from datetime import datetime

from rich.panel import Panel
from rich.text import Text

from ...core.interfaces.selector import SelectorNotSupported, SelectorOption
from ...infrastructure.selectors import get_default_selector
from ..base import CommandContext, CommandResult, DirectCommand
from ._conv_store import ConversationNameStore
from ._shared import wrap_command_errors

_SENTINEL = "_switch_session"
_MAX_LABEL = 72


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _truncate(s: str, n: int = _MAX_LABEL) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


class ResumeCommand(DirectCommand):
    """Select a past conversation and continue from where it left off.

    Conversations are loaded from episodic memory (SQLite), so they survive
    process restarts.  Select with ↑↓ Enter; ESC to cancel.
    """

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="resume",
                description="List and reload a past conversation from episodic memory.",
            )
        )

    @wrap_command_errors("resume")
    async def execute(self, context: CommandContext) -> CommandResult:
        agent = context.agent
        session = context.session
        if agent is None or session is None:
            return CommandResult.error_result("Agent or session not available.")

        memory_manager = getattr(agent, "memory_manager", None)
        if memory_manager is None:
            return CommandResult.error_result("MemoryManager não disponível.")

        episodic = getattr(memory_manager, "episodic_memory", None)
        if episodic is None:
            return CommandResult.error_result("EpisodicMemory não disponível.")

        sessions = await episodic.list_sessions(max_sessions=50)
        if not sessions:
            return CommandResult(
                success=True,
                content=Panel(
                    Text(
                        "Nenhuma conversa encontrada na memória episódica.\n"
                        "As conversas ficam disponíveis após a primeira interação.",
                        style="dim",
                    ),
                    title="Resume",
                    border_style="yellow",
                ),
            )

        store = ConversationNameStore()
        selector = get_default_selector()

        if not selector.is_supported():
            lines = []
            for row in sessions[:20]:
                name = store.get(row["session_id"]) or _truncate(
                    row["first_user_input"], 50
                )
                ts = _fmt_time(row["last_activity"])
                lines.append(f"{ts}  {name}  ({row['episode_count']} msg)")
            return CommandResult(
                success=False,
                content=Panel(
                    Text(
                        "Seletor interativo não disponível (sem TTY).\n\n"
                        + "\n".join(lines),
                        style="yellow",
                    ),
                    title="Resume — sem TTY",
                    border_style="yellow",
                ),
            )

        options = []
        for row in sessions:
            sid = row["session_id"]
            name = store.get(sid) or _truncate(row["first_user_input"], 50)
            ts = _fmt_time(row["last_activity"])
            options.append(
                SelectorOption(
                    label=name,
                    value=sid,
                    description=f"{ts} · {row['episode_count']} msg · {sid}",
                )
            )

        try:
            choice = await selector.select(
                options,
                prompt="Resume — selecione uma conversa (↑↓ Enter ESC):",
                default_index=0,
            )
        except SelectorNotSupported:
            return CommandResult.error_result("Seletor não suportado neste ambiente.")

        if choice is None:
            return CommandResult(
                success=True,
                content=Panel(
                    Text("Resume cancelado.", style="dim"),
                    title="Resume",
                    border_style="yellow",
                ),
                metadata={"cancelled": True},
            )

        target_sid = str(choice.value)
        episodes = await episodic.get_episodes_for_session(target_sid)

        history = []
        for ep in episodes:
            if ep["user_input"]:
                history.append({"role": "user", "content": ep["user_input"], "timestamp": ep["timestamp"], "metadata": {}})
            if ep["agent_response"]:
                history.append({"role": "assistant", "content": ep["agent_response"], "timestamp": ep["timestamp"], "metadata": {}})

        new_sid = f"resume-{int(time.time())}-{target_sid[-8:]}"
        try:
            new_session = agent.create_session(
                session_id=new_sid,
                working_directory=session.working_directory,
            )
        except Exception as exc:
            return CommandResult.error_result(f"Não foi possível criar sessão: {exc}")

        new_session.conversation_history = history

        name = store.get(target_sid) or ""
        if name:
            new_session.context_data["conversation_name"] = name

        session.context_data[_SENTINEL] = new_sid

        label = name or _truncate(episodes[-1]["user_input"]) if episodes else target_sid
        return CommandResult(
            success=True,
            content=Panel(
                Text.from_markup(
                    f"Conversa [bold cyan]{label}[/bold cyan] carregada.\n"
                    f"[dim]{len(history)} mensagem(ns) · ID: {new_sid}[/dim]"
                ),
                title="[bold green]Resume — conversa carregada[/bold green]",
                border_style="green",
            ),
        )
