"""Textual app skeleton para o shell CLI do DEILE — Fase 0+1 da issue #317.

Resolve em parte a issue #307 (resize do terminal). PR #312 entregou o "Nivel 1
pragmatico" (Live perpetuo em comandos opt-in, Rule entre turnos), mas conteudo
ja commitado ao scrollback nao reflowa em resize porque vive no buffer do
emulador, fora do alcance da aplicacao. A solucao definitiva e refatorar o
shell para o framework Textual, onde TODA a UI vive dentro de um App reativo
cuja layout engine adapta a SIGWINCH em qualquer parte da tela.

Esta entrega (Fase 0 + Fase 1 minima):
  - DEILEApp + ChatScreen + CSS .tcss adaptativo
  - Header com identidade do processo (lendo InstanceState quando disponivel)
  - RichLog#chat_history como historico reflowable (substitui o scrollback)
  - Input#prompt com handler de submit (apenas eco no historico — sem
    integracao com process_input_stream nesta fase; vem na Fase 2)
  - Footer com hint de saida e placeholder de stats
  - Falha graciosa quando ``textual`` nao esta instalado (mensagem clara)

Fases 2-6 (integracao com agente, comandos slash, sub-DEILEs, polimento,
cleanup) sao tracked como follow-ups separados na issue #317. Esta entrega
nao remove o shell legado: a flag ``--ui textual`` em ``deile/cli.py`` e
opt-in; default permanece o shell atual.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

_TEXTUAL_AVAILABLE = True
_TEXTUAL_IMPORT_ERROR: Optional[ImportError] = None
try:
    from textual import on
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.screen import Screen
    from textual.widgets import Footer, Header, Input, RichLog
except ImportError as exc:  # pragma: no cover — exercised when extra absent
    _TEXTUAL_AVAILABLE = False
    _TEXTUAL_IMPORT_ERROR = exc


__all__ = [
    "TEXTUAL_AVAILABLE",
    "TEXTUAL_IMPORT_ERROR",
    "TEXTUAL_INSTALL_HINT",
    "ensure_textual_available",
    "DEILEApp",
    "ChatScreen",
    "run_textual_app",
]

TEXTUAL_AVAILABLE: bool = _TEXTUAL_AVAILABLE
TEXTUAL_IMPORT_ERROR: Optional[ImportError] = _TEXTUAL_IMPORT_ERROR

TEXTUAL_INSTALL_HINT = (
    "O framework Textual nao esta instalado. Para usar a UI Textual:\n"
    '    pip install -e ".[ui]"\n'
    "ou, se ja tem o DEILE instalado:\n"
    "    pip install 'textual>=0.80'\n"
    "Veja issue #317 e docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md (UI)."
)


def ensure_textual_available() -> None:
    """Levanta ``ImportError`` com instrucao de install se textual nao existe.

    Chamado pelo entry point antes de instanciar :class:`DEILEApp`. Mantem
    a checagem em um unico lugar e evita que callers caiam num erro de
    import obscuro de dentro do pacote.
    """
    if not TEXTUAL_AVAILABLE:
        raise ImportError(TEXTUAL_INSTALL_HINT) from TEXTUAL_IMPORT_ERROR


# Path para o CSS sidecar (mesma pasta deste modulo).
_CSS_FILE = Path(__file__).with_suffix(".tcss")


def _snapshot_instance_state() -> Dict[str, Any]:
    """Retorna snapshot defensivo do InstanceState do processo, ou {}.

    Best-effort: a Fase 1 deste refactor nao requer InstanceState para
    funcionar (alguns ambientes de teste nem instanciam o singleton). Quando
    indisponivel, o Header degrada para placeholders. Issue #303 ja garante
    que o singleton e barato de criar — o try/except aqui cobre o caso de
    instanciacao bloqueada (filesystem read-only, sandbox sem $HOME, etc.).
    """
    try:
        from deile.runtime.instance_state import get_instance_state
        return get_instance_state().snapshot()
    except Exception:  # noqa: BLE001 — Header is best-effort
        return {}


def _format_header_subtitle(snap: Dict[str, Any]) -> str:
    """Constroi a subtitle do Header a partir do snapshot do InstanceState.

    Formato: ``role=<role> | action=<kind:detail> | turns=<N>``. Mantemos
    curto porque o Header tem 1 linha e ja exibe o titulo na esquerda.
    """
    role = snap.get("role") or "?"
    action = snap.get("current_action") or {}
    kind = action.get("kind") if isinstance(action, dict) else None
    detail = action.get("detail") if isinstance(action, dict) else None
    action_str = kind or "idle"
    if kind and detail:
        action_str = f"{kind}:{detail}"
    stats = snap.get("stats") or {}
    turns = stats.get("turns", 0) if isinstance(stats, dict) else 0
    return f"role={role} | action={action_str} | turns={turns}"


# Quando Textual nao esta instalado, definimos shims minimos pra que o
# ``from .textual_app import DEILEApp`` no CLI nao quebre o import time.
# A checagem de disponibilidade fica em ``ensure_textual_available``; este
# bloco mantem os simbolos importaveis para introspeccao/teste.
if not TEXTUAL_AVAILABLE:  # pragma: no cover — exercised in no-textual envs

    class ChatScreen:  # type: ignore[no-redef]
        """Stub — Textual nao instalado. Use ensure_textual_available()."""

    class DEILEApp:  # type: ignore[no-redef]
        """Stub — Textual nao instalado. Use ensure_textual_available()."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            ensure_textual_available()

else:

    class ChatScreen(Screen):  # type: ignore[misc, no-redef]
        """Tela principal: chat reflowable + input fixo no rodape.

        Layout (definido em ``textual_app.tcss``)::

            Header                      <- 1 linha, top-docked
            ┌─ Vertical#main ──────────┐
            │  RichLog#chat_history    │  <- 1fr, scrollavel, reflowa em resize
            │  Input#prompt            │  <- auto height, bottom-docked
            └──────────────────────────┘
            Footer                      <- 1 linha, bottom-docked

        Issue #317 Fase 1: o handler ``on_input`` apenas echo a entrada no
        ``RichLog``. A integracao real com ``DeileAgent.process_input_stream``
        e tracked como Fase 2 (issue follow-up).
        """

        BINDINGS = [
            ("ctrl+q", "quit_app", "Sair"),
            ("ctrl+l", "clear_log", "Limpar historico"),
        ]

        def compose(self) -> "ComposeResult":  # type: ignore[name-defined]
            yield Header(show_clock=False)
            with Vertical(id="main"):
                yield RichLog(
                    id="chat_history",
                    highlight=True,
                    markup=True,
                    wrap=True,
                    auto_scroll=True,
                )
                yield Input(
                    id="prompt",
                    placeholder="Digite uma mensagem (Ctrl+Q para sair)...",
                )
            yield Footer()

        def on_mount(self) -> None:
            """Foca o input no boot e renderiza a mensagem de boas-vindas."""
            self.query_one("#prompt", Input).focus()
            log = self.query_one("#chat_history", RichLog)
            log.write(
                "[bold cyan]DEILE — shell Textual (Fase 1 skeleton)[/bold cyan]"
            )
            log.write(
                "[dim]Esta fase ainda nao integra o agente. Mensagens sao "
                "apenas ecoadas no historico abaixo.[/dim]"
            )
            log.write(
                "[dim]Issue #317 — Fase 2+ traz a integracao com "
                "process_input_stream.[/dim]"
            )
            log.write("")

        @on(Input.Submitted, "#prompt")
        def _handle_submit(self, event: "Input.Submitted") -> None:  # type: ignore[name-defined]
            """Eco o input no historico e limpa o campo.

            Fase 1: implementacao minima — apenas registra a mensagem do
            usuario no ``RichLog``. Fase 2 substituira o corpo por
            ``await agent.process_input_stream(...)`` iterando os eventos
            e atualizando um container de streaming dedicado.
            """
            text = (event.value or "").strip()
            if not text:
                return
            log = self.query_one("#chat_history", RichLog)
            log.write(f"[bold]>[/bold] {text}")
            log.write(
                "[yellow](integracao com o agente vira na Fase 2 — "
                "issue #317)[/yellow]"
            )
            log.write("")
            event.input.value = ""

        def action_clear_log(self) -> None:
            """Binding Ctrl+L: limpa o historico (mas mantem a app rodando)."""
            self.query_one("#chat_history", RichLog).clear()

        def action_quit_app(self) -> None:
            """Binding Ctrl+Q: encerra a app limpamente."""
            self.app.exit()

    class DEILEApp(App):  # type: ignore[misc, no-redef]
        """Shell Textual do DEILE (esqueleto — Fase 1 da issue #317).

        Layout reativo: ``Header``, ``ChatScreen`` (RichLog + Input), ``Footer``.
        Toda a UI vive dentro deste ``App``, entao SIGWINCH gera re-layout
        automatico via Textual em qualquer parte da tela — incluindo o
        historico ja renderizado (resolve a limitacao fundamental do
        scrollback documentada na issue #307).
        """

        CSS_PATH = str(_CSS_FILE)
        TITLE = "DEILE"
        SUB_TITLE = "shell Textual (skeleton)"

        def __init__(
            self,
            *,
            instance_state_snapshot: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            # Snapshot injetavel pra facilitar teste sem singleton real;
            # quando None, lemos via ``_snapshot_instance_state``.
            self._snap_override = instance_state_snapshot

        def on_mount(self) -> None:
            """Push da ChatScreen + ajuste de subtitle conforme InstanceState."""
            snap = (
                self._snap_override
                if self._snap_override is not None
                else _snapshot_instance_state()
            )
            self.sub_title = _format_header_subtitle(snap)
            self.push_screen(ChatScreen())


def run_textual_app() -> int:
    """Entry point sincrono — instancia e roda :class:`DEILEApp`.

    Chamado por ``deile/cli.py`` quando o usuario passa ``--ui textual``.
    Retorna o exit code (0 = sucesso).
    """
    ensure_textual_available()
    app = DEILEApp()
    app.run()
    return 0
