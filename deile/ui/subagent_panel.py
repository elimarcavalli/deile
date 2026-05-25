"""Renderer multipanel para sub-DEILEs paralelos (issue #257).

Painel ao vivo (Rich :class:`Live`) com N blocos de ~5 linhas, atualização a
~6 Hz, navegação por teclado (``1``-``9`` foca uma frente, ``ESC`` volta /
sai). Encerra sozinho quando todos os ``SubAgentState.is_terminal``.

Notas:
  * O console interno usa ``file=real_stdout`` (capturado pelo orquestrador
    antes do redirect de ``sys.stdout``) — pinta no terminal REAL mesmo
    enquanto ``print()`` em sub-DEILEs está suprimido.
  * Suspende cooperativamente o ``Live`` do streaming_renderer pai (Rich só
    permite um Live ativo por console).
  * Parser de ESC distingue ESC genuíno de prefixo de escape-sequence (setas)
    com timeout de 200ms.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from typing import List, Optional, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape as _rich_escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.orchestration.subagents.events import SubAgentEvent, SubAgentState

logger = logging.getLogger(__name__)


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_REFRESH_HZ = 6.0
# 200ms é o padrão clássico de editores curses — cobre USB em rajada sem ESC
# perceptível.
_ESC_SEQUENCE_TIMEOUT_S = 0.20
_ESC_SEQUENCE_DRAIN_S = 0.05


_STATUS_GLYPH = {
    "pending": "·", "running": "▶",
    "ok": "✅", "error": "❌", "cancelled": "⏹",
}
_STATUS_STYLE = {
    "pending": "dim", "running": "cyan",
    "ok": "green", "error": "red", "cancelled": "yellow",
}


def _fmt_mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class SubAgentPanelRenderer:
    """Live multipanel + entrada de teclado simples (foco e cancel).

    Args:
        host_console: console do streaming_renderer pai. Usado APENAS para
            detectar e suspender o Live do pai durante o painel.
        states: estados (mutáveis pelos runners) a renderizar.
        broadcast: bus interno do orquestrador (subscreve pra acordar mais
            cedo em milestones — refresh ainda corre por timer).
        real_stdout: handle ao stdout *real* (capturado antes do redirect de
            ``sys.stdout`` feito pelo orquestrador). ``None`` cai para o stdout
            corrente (modo headless / testes).
        refresh_hz: frequência mínima de redraw.
        enable_keyboard: ``False`` desabilita o watcher (testes).
    """

    def __init__(
        self,
        host_console: Console,
        states: List[SubAgentState],
        broadcast: Optional[object] = None,
        *,
        real_stdout: Optional[TextIO] = None,
        refresh_hz: float = _REFRESH_HZ,
        enable_keyboard: bool = True,
    ) -> None:
        self._host_console = host_console
        self._panel_console = (
            Console(file=real_stdout, force_terminal=True)
            if real_stdout is not None else host_console
        )
        self._states = states
        self._broadcast = broadcast
        self._refresh_hz = max(1.0, float(refresh_hz))
        self._enable_keyboard = enable_keyboard
        # foco: None = vista compacta; 1..N = ficha da frente N.
        self._focus: Optional[int] = None
        self._frame: int = 0
        self._cancel_requested: bool = False
        self._start_t: float = 0.0
        self._wake = asyncio.Event()
        if self._broadcast is not None and hasattr(self._broadcast, "subscribe"):
            self._broadcast.subscribe(self._on_event)

    @property
    def cancelled(self) -> bool:
        return self._cancel_requested

    def _on_event(self, _evt: SubAgentEvent) -> None:
        try:
            self._wake.set()
        except Exception:
            pass

    # ----- Layouts -----------------------------------------------------------

    def _compose_compact(self) -> Group:
        """Vista compacta: 1 painel por sub-DEILE, com espaçamento entre eles."""
        items: List = [self._header_renderable(), Text("")]
        last = len(self._states) - 1
        for i, st in enumerate(self._states):
            items.append(self._panel_for(st))
            if i < last:
                items.append(Text(""))
        items.append(Text(""))
        hint = Text(
            "(toque 1-9 para focar · ESC: fecha painel)" if self._enable_keyboard
            else "(painel multipanel)",
            style="dim",
        )
        items.append(hint)
        return Group(*items)

    def _compose_focus(self, idx: int) -> Group:
        """Layout foco: ficha completa da frente ``idx`` + tail de execução."""
        if not (1 <= idx <= len(self._states)):
            return self._compose_compact()
        st = self._states[idx - 1]
        hint = Text("(ESC: voltar · ←/→: outra frente)", style="dim")
        return Group(
            self._header_renderable(), Text(""),
            self._ficha_for(st), Text(""),
            self._execution_block(st), Text(""),
            hint,
        )

    def _header_renderable(self) -> Text:
        n = len(self._states)
        running = sum(1 for s in self._states if s.status == "running")
        done = sum(1 for s in self._states if s.is_terminal)
        ok = sum(1 for s in self._states if s.status == "ok")
        err = sum(1 for s in self._states if s.status in ("error", "cancelled"))
        spinner = _SPINNER[self._frame % len(_SPINNER)] if running else "🧩"
        elapsed = _fmt_mmss(time.monotonic() - self._start_t) if self._start_t else "00:00"
        return Text.from_markup(
            f"[bold cyan]{spinner}[/bold cyan] "
            f"[bold]Decomposto em {n} frentes paralelas[/bold] · "
            f"[green]{ok} ok[/green] · "
            + (f"[red]{err} erro[/red] · " if err else "")
            + f"[dim]{done}/{n} concluídas · {elapsed}[/dim]"
        )

    def _panel_for(self, st: SubAgentState) -> Panel:
        """~5 linhas de status para uma frente, no layout compacto."""
        status = st.status
        style = _STATUS_STYLE.get(status, "white")
        glyph = _STATUS_GLYPH.get(status, "•")
        elapsed = _fmt_mmss(st.elapsed_s)

        # ``description`` vem do payload da tool (LLM-supplied) — escape p/ evitar
        # quebra de markup do painel.
        title = (
            f"[{style}]{glyph}[/{style}] "
            f"[bold]sub-DEILE #{st.task.index}[/bold] · "
            f"{_escape_markup(_truncate(st.task.description, 56))} "
            f"[dim]{elapsed}[/dim]"
        )

        # Estado terminal colapsa em 1-linha; senão até 3 últimas progress + activity.
        if st.is_terminal:
            tail = _files_tail(st.files_touched, head=3)
            if status == "ok":
                body_lines = [f"[green]✅ concluído[/green]{tail}"]
            elif status == "error":
                body_lines = [
                    f"[red]❌ {_escape_markup(_truncate(st.error or 'erro', 70))}[/red]"
                ]
            else:
                body_lines = ["[yellow]⏹ cancelado[/yellow]"]
        else:
            body_lines = [
                _escape_markup(_truncate(line, 70))
                for line in list(st.progress_lines)[-3:]
            ]
            if st.current_activity and (
                not body_lines or body_lines[-1] != _escape_markup(_truncate(st.current_activity, 70))
            ):
                body_lines.append("… " + _escape_markup(_truncate(st.current_activity, 70)))
            if not body_lines:
                body_lines = [
                    "[dim]aguardando…[/dim]" if status == "pending"
                    else "[dim](sem atividade ainda)[/dim]"
                ]

        return Panel(
            Text.from_markup("\n".join(body_lines)),
            title=Text.from_markup(title),
            title_align="left",
            border_style=style,
            padding=(0, 1),
        )

    def _ficha_for(self, st: SubAgentState) -> Panel:
        """Ficha de identidade da frente focada."""
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column()
        t.add_row("description", _escape_markup(_truncate(st.task.description, 80)))
        t.add_row("subagent_type", _escape_markup(st.task.persona or "developer (default)"))
        t.add_row(
            "model",
            Text.from_markup(
                "[dim](herdado da sessão)[/dim]" if not st.task.model
                else _escape_markup(st.task.model)
            ),
        )
        status_line = (
            f"[{_STATUS_STYLE.get(st.status, 'white')}]"
            f"{_STATUS_GLYPH.get(st.status, '•')} {st.status}"
            f"[/] · {_fmt_mmss(st.elapsed_s)}"
        )
        if st.task_id:
            status_line += f" · task_id={_escape_markup(st.task_id)}"
        t.add_row("status", Text.from_markup(status_line))
        if st.files_touched:
            files = ", ".join(st.files_touched[:6])
            if len(st.files_touched) > 6:
                files += f" (+{len(st.files_touched) - 6})"
            t.add_row("files", _escape_markup(_truncate(files, 80)))
        prompt_lines = st.task.prompt.splitlines()[:6]
        prompt_show = "\n".join(prompt_lines)
        if len(st.task.prompt.splitlines()) > 6:
            prompt_show += "\n  […]"
        t.add_row("prompt", _escape_markup(_truncate(prompt_show, 320)))
        return Panel(
            t,
            title=f"sub-DEILE #{st.task.index} — ficha",
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    def _execution_block(self, st: SubAgentState) -> Panel:
        """Tail das últimas N linhas de progresso (modo foco)."""
        lines = list(st.progress_lines)[-12:]
        if not lines and st.current_activity:
            lines = [st.current_activity]
        body = Text("\n".join(lines)) if lines else Text("(sem atividade ainda)", style="dim")
        return Panel(
            body, title="execução (snapshot)",
            title_align="left", border_style="dim", padding=(0, 1),
        )

    # ----- Loop principal ----------------------------------------------------

    async def run(self) -> None:
        """Renderiza enquanto houver state não-terminal. Não levanta (exceto CancelledError)."""
        self._start_t = time.monotonic()
        # Rich só permite um Live ativo por console — suspender o pai antes.
        prev_live = _safe_get_parent_live(self._host_console)
        if prev_live is not None:
            try:
                prev_live.stop()
            except Exception:
                logger.debug("Falha ao suspender Live do pai", exc_info=True)

        # Stdin precisa de exclusão mútua com o ESC watcher do CLI.
        from deile.ui._stdin_owner import (claim_stdin_for_panel,
                                           release_stdin_for_panel)

        kb_stop = threading.Event()
        kb_thread: Optional[threading.Thread] = None
        stdin_claimed = False
        if self._enable_keyboard:
            try:
                claim_stdin_for_panel()
                stdin_claimed = True
            except Exception:
                logger.debug("claim_stdin_for_panel failed", exc_info=True)
            kb_thread = self._start_keyboard_watcher(kb_stop)

        period = 1.0 / self._refresh_hz
        try:
            # ``redirect_stdout/stderr=False``: Rich Live, por padrão, faz
            # ``sys.stdout = FileProxy(console)`` para que ``print()``
            # durante a Live aparcça acima da região. AQUI isso é tóxico:
            # o orquestrador já redirecionou sys.stdout para um buffer
            # (suprimindo print() de sub-DEILEs), e Live SOBRESCREVERIA esse
            # redirect, mandando print() do bash_tool diretamente para
            # ``panel_console.file`` (= terminal real) — gera leak visível
            # (issue #257 round 5). Mantemos o redirect do orquestrador
            # intacto desabilitando o do Live.
            with Live(
                self._render_frame(),
                console=self._panel_console,
                refresh_per_second=self._refresh_hz,
                transient=False,
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
            ) as live:
                while True:
                    self._frame += 1
                    live.update(self._render_frame())
                    live.refresh()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=period)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    if self._cancel_requested:
                        break
                    if all(s.is_terminal for s in self._states):
                        live.update(self._render_frame())
                        live.refresh()
                        break

                live.update(self._final_summary())
                live.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SubAgentPanelRenderer crashed")
        finally:
            kb_stop.set()
            if kb_thread is not None and kb_thread.is_alive():
                # Daemon thread — não bloqueia shutdown; 200ms é cortesia.
                kb_thread.join(timeout=0.2)
            if stdin_claimed:
                try:
                    release_stdin_for_panel()
                except Exception:
                    logger.debug("release_stdin_for_panel failed", exc_info=True)
            if prev_live is not None:
                try:
                    prev_live.start(refresh=True)
                except Exception:
                    logger.debug("Falha ao restaurar Live do pai", exc_info=True)

    def _render_frame(self):
        if self._focus is None:
            return self._compose_compact()
        return self._compose_focus(self._focus)

    def _final_summary(self) -> Group:
        """1 linha por sub-DEILE no fechamento — vai pra scrollback."""
        rows: List = [
            Text.from_markup(
                f"[bold cyan]🧩 Sub-DEILEs concluídos[/bold cyan] · "
                f"{_fmt_mmss(time.monotonic() - self._start_t)} total"
            ),
            Text(""),
        ]
        for st in self._states:
            glyph = _STATUS_GLYPH.get(st.status, "•")
            style = _STATUS_STYLE.get(st.status, "white")
            tail = _files_tail(st.files_touched, head=5)
            elapsed = _fmt_mmss(st.elapsed_s)
            desc = _escape_markup(_truncate(st.task.description, 56))
            rows.append(Text.from_markup(
                f"  [{style}]{glyph}[/{style}] #{st.task.index} {desc} "
                f"[dim]({elapsed}){tail}[/dim]"
            ))
        return Group(*rows)

    # ----- Keyboard (cbreak via thread daemon) -------------------------------

    def _apply_key(self, seq: str) -> bool:
        """Processa uma sequência de teclado; retorna ``True`` se houve mudança."""
        n_states = len(self._states)
        if seq == "\x1b":
            if self._focus is not None:
                self._focus = None
            else:
                self._cancel_requested = True
        elif len(seq) == 1 and seq.isdigit() and seq != "0":
            idx = int(seq)
            if 1 <= idx <= n_states:
                self._focus = idx
            else:
                return False
        elif seq in ("\x1b[D", "\x1bOD", "h"):  # left
            if self._focus and self._focus > 1:
                self._focus -= 1
            else:
                return False
        elif seq in ("\x1b[C", "\x1bOC", "l"):  # right
            if self._focus and self._focus < n_states:
                self._focus += 1
            else:
                return False
        else:
            return False
        return True

    def _start_keyboard_watcher(self, stop_event: threading.Event) -> Optional[threading.Thread]:
        try:
            import select as _select
            import termios
            import tty
        except ImportError:
            return None

        if not sys.stdin.isatty():
            return None

        # Não setcbreak se já estamos em cbreak (CLI watcher já entrou) — evita
        # double-restore. atexit em ``_stdin_owner`` é rede de segurança.
        try:
            fd = sys.stdin.fileno()
            current_attrs = termios.tcgetattr(fd)
            already_cbreak = not bool(current_attrs[3] & termios.ICANON)
        except Exception:
            already_cbreak = True
            current_attrs = None
        we_set_cbreak = False
        if not already_cbreak and current_attrs is not None:
            try:
                tty.setcbreak(sys.stdin.fileno())
                we_set_cbreak = True
            except Exception:
                logger.debug("setcbreak failed; keyboard watcher disabled", exc_info=True)
                return None

        loop = asyncio.get_running_loop()

        def _wake_loop():
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

        def _read_byte() -> Optional[str]:
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                return None
            return ch

        def _drain_escape_sequence(intro: str) -> str:
            """Lê o resto da escape-sequence após ``\\x1b<intro>`` (`[` ou `O`)."""
            seq = "\x1b" + intro
            deadline = time.monotonic() + _ESC_SEQUENCE_DRAIN_S
            while time.monotonic() < deadline and len(seq) < 8:
                r, _, _ = _select.select([sys.stdin], [], [], 0.005)
                if not r:
                    break
                nxt = _read_byte()
                if not nxt:
                    break
                seq += nxt
                # CSI termina em uma letra A-Z/a-z; SS3 (``\x1bO``) é sempre 3 bytes.
                if intro == "O" or (intro == "[" and 0x40 <= ord(nxt) <= 0x7e):
                    break
            return seq

        def _watch() -> None:
            try:
                while not stop_event.is_set():
                    r, _, _ = _select.select([sys.stdin], [], [], 0.1)
                    if not r:
                        continue
                    ch = _read_byte()
                    if ch is None:
                        break
                    if not ch:
                        continue
                    if ch != "\x1b":
                        if self._apply_key(ch):
                            _wake_loop()
                        continue

                    # ESC: pode ser solo ou prefixo de escape-sequence.
                    r2, _, _ = _select.select([sys.stdin], [], [], _ESC_SEQUENCE_TIMEOUT_S)
                    if not r2:
                        if self._apply_key("\x1b"):
                            _wake_loop()
                        continue
                    intro = _read_byte()
                    if intro is None:
                        break
                    if intro not in ("[", "O"):
                        # ESC + algo inesperado: processa cada um separadamente.
                        changed = self._apply_key("\x1b") | self._apply_key(intro)
                        if changed:
                            _wake_loop()
                        continue
                    seq = _drain_escape_sequence(intro)
                    if self._apply_key(seq):
                        _wake_loop()
            except Exception:
                logger.debug("keyboard watcher crashed", exc_info=True)
            finally:
                # Só restaura se NÓS setamos cbreak.
                if we_set_cbreak and current_attrs is not None:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, current_attrs)
                    except Exception:
                        pass

        t = threading.Thread(target=_watch, daemon=True, name="subagent-kb-watcher")
        t.start()
        return t


def _safe_get_parent_live(console) -> Optional[object]:
    """Best-effort lookup do ``Live`` ativo. Rich 13.x guarda em ``Console._live``
    (privado); se a API mudar, retornamos ``None`` e o caller tolera ausência."""
    try:
        return getattr(console, "_live", None)
    except Exception:
        logger.debug("Could not access Console._live (Rich API drift)", exc_info=True)
        return None


def _files_tail(files: list, *, head: int) -> str:
    """``" · a, b, c (+N)"`` ou string vazia."""
    if not files:
        return ""
    head_files = ", ".join(files[:head])
    tail = f" · {_escape_markup(head_files)}"
    if len(files) > head:
        tail += f" (+{len(files) - head})"
    return tail


def _truncate(text, limit: int) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _escape_markup(text) -> str:
    """Escapa ``[…]`` em texto não-confiável (progress_lines, output de tools).

    Sem isso, um ``[red]`` no output de bash quebraria a renderização do Panel.
    """
    if text is None:
        return ""
    return _rich_escape(str(text))


__all__ = ["SubAgentPanelRenderer"]
