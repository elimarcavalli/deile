"""Renderer multipanel para sub-DEILEs paralelos (issue #257).

Painel ao vivo (Rich :class:`Live`) com N blocos de ~5 linhas, atualização a
~6 Hz, navegação por teclado (``1``-``9`` foca uma frente, ``ESC`` volta /
sai). Encerra sozinho quando todos os ``SubAgentState.is_terminal``.

Convivência com a ``StreamingRenderer`` principal
-------------------------------------------------

A CLI já tem um ``rich.live.Live`` ativo durante o turno (pertence à
:class:`deile.ui.streaming_renderer.StreamingRenderer`). Rich permite apenas
um ``Live`` por console; ao iniciar o nosso, **suspendemos** o ``Live`` do pai
(``console._live``) e o restauramos no ``finally``. A tool
``dispatch_parallel_subagents`` está em ``_DIRECT_PRINT_TOOLS`` da
StreamingRenderer, então o cabeçalho do tool call vai pra scrollback ANTES
da execução — o painel abre logo abaixo, sem colidir com o cabeçalho.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from typing import List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.orchestration.subagents.events import (SubAgentEvent,
                                                  SubAgentState)

logger = logging.getLogger(__name__)


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_REFRESH_HZ = 6.0


_STATUS_GLYPH = {
    "pending": "·",
    "running": "▶",
    "ok": "✅",
    "error": "❌",
    "cancelled": "⏹",
}

_STATUS_STYLE = {
    "pending": "dim",
    "running": "cyan",
    "ok": "green",
    "error": "red",
    "cancelled": "yellow",
}


def _fmt_mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class SubAgentPanelRenderer:
    """Live multipanel + entrada de teclado simples (foco e cancel).

    Uso típico:

        renderer = SubAgentPanelRenderer(console, states, broadcast)
        await renderer.run()    # bloqueia até todos os states virarem terminal

    O orquestrador agenda :meth:`run` como ``asyncio.Task`` em paralelo aos
    runners — ver :class:`SubAgentOrchestrator`. O renderer NUNCA cancela
    runners por conta própria; cancel manual via ESC marca um flag que
    :meth:`should_cancel` expõe, mas a decisão de cancelar é do orquestrador
    (no MVP, apenas fechamos o painel ao receber ESC global; o trabalho
    continua em background).
    """

    def __init__(
        self,
        console: Console,
        states: List[SubAgentState],
        broadcast: Optional[object] = None,
        *,
        refresh_hz: float = _REFRESH_HZ,
        enable_keyboard: bool = True,
    ) -> None:
        self._console = console
        self._states = states
        self._broadcast = broadcast  # subscribe(cb) usado por orquestrador
        self._refresh_hz = max(1.0, float(refresh_hz))
        self._enable_keyboard = enable_keyboard
        # Foco: None = vista compacta; 1..N = ficha da frente N.
        self._focus: Optional[int] = None
        self._frame: int = 0
        self._cancel_requested: bool = False
        self._start_t: float = 0.0
        # subscribe-se ao broadcast só para acordar mais cedo em milestones
        # importantes; o desenho ocorre pelo loop de refresh.
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
        """Vista compacta: 1 painel por sub-DEILE."""
        header = self._header_renderable()
        panels: List[Panel] = []
        for st in self._states:
            panels.append(self._panel_for(st))
        hint = Text(
            "(toque 1-9 para focar · ESC: fecha painel)" if self._enable_keyboard
            else "(painel multipanel)",
            style="dim",
        )
        return Group(header, Text(""), *panels, hint)

    def _compose_focus(self, idx: int) -> Group:
        """Layout foco: ficha completa da frente ``idx`` + tail de execução."""
        if not (1 <= idx <= len(self._states)):
            return self._compose_compact()
        st = self._states[idx - 1]
        header = self._header_renderable()
        ficha = self._ficha_for(st)
        execution = self._execution_block(st)
        hint = Text(
            "(ESC: voltar · ←/→: outra frente)",
            style="dim",
        )
        return Group(header, Text(""), ficha, execution, hint)

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

        # Title: status glyph + descrição + tempo
        title = (
            f"[{style}]{glyph}[/{style}] "
            f"[bold]sub-DEILE #{st.task.index}[/bold] · "
            f"{_truncate(st.task.description, 56)} "
            f"[dim]{elapsed}[/dim]"
        )

        # Corpo: até 3 últimas linhas de progresso + current_activity
        body_lines: List[str] = []
        recent = list(st.progress_lines)[-3:]
        for line in recent:
            body_lines.append(_truncate(line, 70))
        # Always show current_activity at the bottom if present and not duplicate
        if st.current_activity and (not recent or recent[-1] != st.current_activity):
            body_lines.append(_truncate("… " + st.current_activity, 70))
        if not body_lines:
            if status == "pending":
                body_lines.append("[dim]aguardando…[/dim]")
            else:
                body_lines.append("[dim](sem atividade ainda)[/dim]")

        # Final state collapses to a 1-line summary
        if st.is_terminal:
            files = ", ".join(st.files_touched[:3])
            tail = ""
            if files:
                tail = f" · {files}"
                if len(st.files_touched) > 3:
                    tail += f" (+{len(st.files_touched) - 3})"
            if status == "ok":
                body_lines = [f"[green]✅ concluído[/green]{tail}"]
            elif status == "error":
                body_lines = [f"[red]❌ {_truncate(st.error or 'erro', 70)}[/red]"]
            else:
                body_lines = [f"[yellow]⏹ cancelado[/yellow]"]

        body = Text.from_markup("\n".join(body_lines))
        return Panel(
            body,
            title=Text.from_markup(title),
            title_align="left",
            border_style=style,
            padding=(0, 1),
        )

    def _ficha_for(self, st: SubAgentState) -> Panel:
        """Ficha de identidade da frente focada (modo foco)."""
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column()
        t.add_row("description", _truncate(st.task.description, 80))
        t.add_row("subagent_type", st.task.persona or "developer (default)")
        t.add_row("model", st.task.model or "[dim](herdado da sessão)[/dim]")
        status_line = (
            f"[{_STATUS_STYLE.get(st.status, 'white')}]"
            f"{_STATUS_GLYPH.get(st.status, '•')} {st.status}"
            f"[/] · {_fmt_mmss(st.elapsed_s)}"
        )
        if st.task_id:
            status_line += f" · task_id={st.task_id}"
        t.add_row("status", Text.from_markup(status_line))
        if st.files_touched:
            files = ", ".join(st.files_touched[:6])
            if len(st.files_touched) > 6:
                files += f" (+{len(st.files_touched) - 6})"
            t.add_row("files", _truncate(files, 80))
        # prompt: até 6 linhas, indentado
        prompt_lines = st.task.prompt.splitlines()[:6]
        prompt_show = "\n".join(prompt_lines)
        if len(st.task.prompt.splitlines()) > 6:
            prompt_show += "\n  […]"
        t.add_row("prompt", _truncate(prompt_show, 320))
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
        body = Text("\n".join(lines) if lines else "(sem atividade ainda)", style="dim" if not lines else None)
        return Panel(
            body,
            title="execução (snapshot)",
            title_align="left",
            border_style="dim",
            padding=(0, 1),
        )

    # ----- Loop principal ----------------------------------------------------

    async def run(self) -> None:
        """Renderiza enquanto houver state não-terminal. Não levanta exceção."""
        self._start_t = time.monotonic()
        # Suspende o Live do pai (streaming_renderer) — Rich só permite um.
        prev_live = getattr(self._console, "_live", None)
        if prev_live is not None:
            try:
                prev_live.stop()
            except Exception:
                pass
            self._console._live = None

        # Watcher de teclado em thread daemon (igual padrão do cli._stream_with_esc_cancel)
        kb_stop = threading.Event()
        kb_thread: Optional[threading.Thread] = None
        if self._enable_keyboard:
            kb_thread = self._start_keyboard_watcher(kb_stop)

        period = 1.0 / self._refresh_hz
        try:
            with Live(
                self._render_frame(),
                console=self._console,
                refresh_per_second=self._refresh_hz,
                transient=False,
                auto_refresh=False,
            ) as live:
                while True:
                    self._frame += 1
                    live.update(self._render_frame())
                    live.refresh()
                    # Pisca: sleep curto, acorda em milestones.
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=period)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    if self._cancel_requested:
                        break
                    if all(s.is_terminal for s in self._states):
                        # Última frame, para o usuário ver o estado final.
                        live.update(self._render_frame())
                        live.refresh()
                        break

                # Resumo final no scrollback (1 linha por frente).
                live.update(self._final_summary())
                live.refresh()
        except Exception:
            logger.exception("SubAgentPanelRenderer crashed")
        finally:
            kb_stop.set()
            if kb_thread is not None and kb_thread.is_alive():
                # Daemon thread — não bloqueia o shutdown, mas damos 200ms.
                kb_thread.join(timeout=0.2)
            if prev_live is not None:
                try:
                    self._console.set_live(prev_live)
                    prev_live.start(refresh=True)
                except Exception:
                    pass

    def _render_frame(self):
        if self._focus is None:
            return self._compose_compact()
        return self._compose_focus(self._focus)

    def _final_summary(self) -> Group:
        """1 linha por sub-DEILE no fechamento — vai pra scrollback."""
        rows: List[Text] = []
        rows.append(Text.from_markup(
            f"[bold cyan]🧩 Sub-DEILEs concluídos[/bold cyan] · "
            f"{_fmt_mmss(time.monotonic() - self._start_t)} total"
        ))
        for st in self._states:
            glyph = _STATUS_GLYPH.get(st.status, "•")
            style = _STATUS_STYLE.get(st.status, "white")
            files = ", ".join(st.files_touched[:5])
            tail = ""
            if files:
                tail = f" · {files}"
                if len(st.files_touched) > 5:
                    tail += f" (+{len(st.files_touched) - 5})"
            elapsed = _fmt_mmss(st.elapsed_s)
            line = (
                f"  [{style}]{glyph}[/{style}] #{st.task.index} "
                f"{_truncate(st.task.description, 56)} "
                f"[dim]({elapsed}){tail}[/dim]"
            )
            rows.append(Text.from_markup(line))
        return Group(*rows)

    # ----- Keyboard (cbreak via thread daemon) -------------------------------

    def _start_keyboard_watcher(self, stop_event: threading.Event) -> Optional[threading.Thread]:
        try:
            import select as _select
            import termios
            import tty
        except ImportError:
            return None

        if not sys.stdin.isatty():
            return None

        try:
            saved = termios.tcgetattr(sys.stdin.fileno())
        except Exception:
            return None

        loop = asyncio.get_running_loop()

        def _on_key(ch: str) -> None:
            if ch == "\x1b":
                # ESC: se em foco, volta à vista geral. Senão, sinaliza fechamento.
                if self._focus is not None:
                    self._focus = None
                else:
                    self._cancel_requested = True
            elif ch.isdigit() and ch != "0":
                idx = int(ch)
                if 1 <= idx <= len(self._states):
                    self._focus = idx
            elif ch in ("\x1b[D", "h"):  # left arrow / vim h
                if self._focus and self._focus > 1:
                    self._focus -= 1
            elif ch in ("\x1b[C", "l"):  # right arrow / vim l
                if self._focus and self._focus < len(self._states):
                    self._focus += 1
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

        def _watch() -> None:
            try:
                tty.setcbreak(sys.stdin.fileno())
                while not stop_event.is_set():
                    r, _, _ = _select.select([sys.stdin], [], [], 0.1)
                    if not r:
                        continue
                    ch = sys.stdin.read(1)
                    if ch != "\x1b":
                        _on_key(ch)
                        continue
                    # ESC vs escape-sequence (seta etc.)
                    r2, _, _ = _select.select([sys.stdin], [], [], 0.05)
                    if not r2:
                        _on_key("\x1b")
                        continue
                    seq = "\x1b"
                    while _select.select([sys.stdin], [], [], 0.01)[0]:
                        seq += sys.stdin.read(1)
                        if len(seq) >= 6:
                            break
                    _on_key(seq)
            except Exception:
                pass
            finally:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
                except Exception:
                    pass

        t = threading.Thread(target=_watch, daemon=True)
        t.start()
        return t


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


__all__ = ["SubAgentPanelRenderer"]
