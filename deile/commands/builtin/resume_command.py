"""ResumeCommand — /resume: list past conversations and reload one."""

from __future__ import annotations

from datetime import datetime

from rich.panel import Panel
from rich.text import Text

from ...core.interfaces.selector import SelectorNotSupported, SelectorOption
from ...infrastructure.selectors import get_default_selector
from ..base import CommandContext, CommandResult, DirectCommand
from ._conv_store import ConversationNameStore
from ._session_store import SessionHistoryStore
from ._shared import wrap_command_errors

_SENTINEL = "_switch_session"
_POST_SWITCH = "_post_switch_action"
_MAX_LABEL = 72


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _truncate(s: object, n: int = _MAX_LABEL) -> str:
    if not s:
        return ""
    text = str(s).replace("\n", " ").strip()
    return text[:n] + "…" if len(text) > n else text


class ResumeCommand(DirectCommand):
    """Select a past conversation and continue from where it left off.

    Conversations are loaded from ``~/.deile/sessions/`` (written by the CLI
    after each LLM turn).  Select with ↑↓ Enter; ESC to cancel.
    """

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="resume",
                description="List and reload a past conversation.",
            )
        )

    @wrap_command_errors("resume")
    async def execute(self, context: CommandContext) -> CommandResult:
        agent = context.agent
        session = context.session
        if agent is None or session is None:
            return CommandResult.error_result("Agent or session not available.")

        name_store = ConversationNameStore()
        hist_store = SessionHistoryStore()

        sessions = hist_store.list_sessions(max_sessions=50)
        if not sessions:
            return CommandResult(
                success=True,
                content=Panel(
                    Text(
                        "Nenhuma conversa encontrada.\n"
                        "As conversas ficam disponíveis após a primeira interação com o agente.",
                        style="dim",
                    ),
                    title="Resume",
                    border_style="yellow",
                ),
            )

        selector = get_default_selector()

        if not selector.is_supported():
            lines = []
            for row in sessions[:20]:
                name = (
                    name_store.get(row["session_id"])
                    or row.get("conversation_name")
                    or _truncate(row["first_user_input"], 50)
                )
                ts = _fmt_time(row["last_activity"])
                lines.append(f"{ts}  {name}  ({row['message_count']} msg)")
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
            name = (
                name_store.get(sid)
                or row.get("conversation_name")
                or _truncate(row["first_user_input"], 50)
                or sid
            )
            ts = _fmt_time(row["last_activity"])
            options.append(
                SelectorOption(
                    label=name,
                    value=sid,
                    description=f"{ts} · {row['message_count']} msg · {sid}",
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
        stored = hist_store.load(target_sid)
        if stored is None:
            return CommandResult.error_result(f"Conversa {target_sid!r} não encontrada em disco.")

        history = stored.get("history", [])

        # /resume retoma a MESMA sessão (mesmo session_id) — diferente de
        # /rewind, que cria um fork. Se o agent já tem a sessão em memória
        # (mesmo processo, mesma execução), reutilizamos; caso contrário,
        # registramos uma nova entrada com o mesmo id em ``agent._sessions``.
        target_session = agent.get_session(target_sid)
        if target_session is None:
            try:
                target_session = agent.create_session(
                    session_id=target_sid,
                    working_directory=session.working_directory,
                )
            except Exception as exc:
                return CommandResult.error_result(
                    f"Não foi possível registrar a sessão {target_sid!r}: {exc}"
                )

        target_session.conversation_history = [dict(e) for e in history]

        name = (
            name_store.get(target_sid)
            or stored.get("conversation_name", "")
        )
        if name:
            target_session.context_data["conversation_name"] = name

        session.context_data[_SENTINEL] = target_sid
        session.context_data[_POST_SWITCH] = "replay"

        return CommandResult.success_result(
            "",
            "text",
            suppress_response_display=True,
            resumed_session_id=target_sid,
            message_count=len(history),
        )
