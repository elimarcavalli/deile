"""RewindCommand — /rewind: go back to an earlier point in the conversation."""

from __future__ import annotations

from rich.panel import Panel
from rich.text import Text

from ...core.interfaces.selector import SelectorNotSupported, SelectorOption
from ...infrastructure.selectors import get_default_selector
from .._sentinels import POST_SWITCH_ACTION_KEY, SWITCH_SESSION_KEY
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
                content="",
                metadata={
                    "cancelled": True,
                    "suppress_response_display": True,
                },
            )

        # Trunca ATÉ E INCLUSIVE a mensagem selecionada — voltar pra
        # "antes daquela mensagem". Ex.: selecionar #3 apaga a #3 e tudo
        # depois dela, deixando o histórico em [#1, resposta-#1, #2,
        # resposta-#2]. O usuário tem o prompt vazio pronto para
        # reformular a mensagem que estava na posição #3.
        cut = int(choice.value)
        trimmed = history[:cut]

        # Muta o histórico DA SESSÃO ATUAL — não cria fork. Rewind é
        # destrutivo por design (mais próximo do mental model de "voltar
        # atrás" do chat). Para preservar histórico, use /fork antes.
        session.conversation_history = [dict(e) for e in trimmed]

        # Reusa a infra de session-switch para acionar o replay: o CLI
        # vai pop SWITCH+POST_SWITCH em ``check_session_switch``,
        # resolver a sessão (mesmo session_id → mesma sessão), e o
        # branch ``action == "replay"`` chama ``replay_history`` que
        # limpa a tela, redesenha o welcome e re-renderiza o histórico
        # truncado. Sem isso, o scrollback continuaria mostrando tudo.
        session.context_data[SWITCH_SESSION_KEY] = session.session_id
        session.context_data[POST_SWITCH_ACTION_KEY] = "replay"

        # ``suppress_response_display`` evita qualquer Panel/texto antes
        # do replay — qualquer print aqui apareceria por uma fração de
        # segundo antes do ``console.clear()`` do replay, gerando flash
        # visual. O replay já comunica a mudança (tela limpa + histórico
        # menor).
        return CommandResult(
            success=True,
            content="",
            metadata={"suppress_response_display": True},
        )
