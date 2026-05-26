"""Renderer multipanel para sub-DEILEs paralelos (issue #257).

Painel ao vivo (Rich :class:`Live`) com N blocos de ~5 linhas, atualização a
~6 Hz, navegação por teclado (``1``-``9`` foca uma frente, ``ESC`` volta /
sai). Encerra sozinho quando todos os ``SubAgentState.is_terminal``.

Round 2 (post-feedback):
  * Console dedicado com ``file=real_stdout`` (capturado pelo orquestrador
    antes do redirect de sys.stdout) — o painel escreve no terminal REAL
    mesmo enquanto ``print()`` em sub-DEILEs está suprimido.
  * Suspende o ``Live`` do streaming_renderer pai cooperativamente
    (``stop()`` + ``start()``), tolerando ausência (modo headless / fixture).
  * Espaçamento extra entre painéis para legibilidade.
  * Parser de teclado robusto: distingue ESC genuíno de prefixo de seta com
    timeout de 200ms (não 50ms — era apertado demais e levava ``ESC`` a
    disparar quando o usuário pressionava arrows em rajada — issue #257
    feedback ponto 4).

Round 6 (rajada de setas → ESC falso):
  * Parser byte-a-byte do round 2 tinha 3 falhas reais em rajadas:
    (1) ``read(1)`` pode retornar ``""`` em meio à drain (interrompido por
        sinal), e o branch ``intro not in ("[", "O")`` então disparava
        ``_on_key("\\x1b")`` — fechando o painel;
    (2) drain de 50ms com select por byte de 5ms era frágil para teclados
        que entregam bytes em rajada num único batch (kernel buffer);
    (3) entre iterações do loop, se a segunda seta chegasse atrasada (≥200ms
        — pause natural em rajada manual), o ``\\x1b`` solo expirava o
        timeout e ainda disparava cancel.
  * Solução: ler em rajada com ``os.read(fd, N)`` (atômico, pega tudo que
    o kernel já tem) e parsear o buffer com state-machine puro
    (:func:`parse_key_buffer`), testável sem TTY. Bytes pendentes no buffer
    ficam para a próxima iteração — sem timeout artificial pra sequências
    completas. ESC genuíno só dispara quando o ``\\x1b`` chega *isolado* no
    buffer E não há mais bytes em 200ms (timeout só nesse caso).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from typing import List, Optional, TextIO, Tuple

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape as _rich_escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.orchestration.subagents.events import SubAgentEvent, SubAgentState

from ..common.text_utils import truncate
from .spinner import BRAILLE_SPINNER_FRAMES as _SPINNER

logger = logging.getLogger(__name__)



_REFRESH_HZ = 6.0
# Timeout (segundos) após receber ``\x1b`` ISOLADO no buffer para decidir se
# é ESC genuíno ou prefixo de seta. 200ms é a recomendação clássica de
# editores curses; cobre teclados USB em rajada sem deixar ESC perceptível.
_ESC_SEQUENCE_TIMEOUT_S = 0.20
# Tamanho máximo de uma leitura atômica do stdin (``os.read``). 64 cobre
# folgada qualquer rajada plausível de setas/teclas em <200ms (cada seta é
# 3 bytes; >20 teclas/100ms é mais rápido que qualquer humano).
_STDIN_READ_CHUNK = 64


def parse_key_buffer(buf: str) -> Tuple[List[str], str]:
    """Parsea ``buf`` em sequências de tecla reconhecidas.

    Retorna ``(seqs, remainder)`` onde ``seqs`` é a lista de sequências
    completas em ordem de chegada e ``remainder`` é o sufixo do buffer que
    ainda não forma uma sequência completa (deve voltar pra próxima rodada).

    Sequências reconhecidas (cada item de ``seqs`` casa com o que o legacy
    ``_on_key`` espera):

    * ``"\\x1b"``: ESC isolado. **Só emitido quando o caller decide que o
      buffer não vai crescer mais** — ver ``flush=True`` abaixo. Por default
      ESC fica em ``remainder``, aguardando os bytes da sequência.
    * ``"\\x1b[A/B/C/D"``: setas via CSI introducer.
    * ``"\\x1bOA/B/C/D"``: setas via SS3 introducer (terminal alt-mode).
    * ``"\\x1b[<digits><final>"``: outras CSI (PageUp/Down, Home/End com
      modificadores); ignoradas pelo caller.
    * 1 char ASCII printable (dígito, letra) cada um vira sua própria seq.

    Bytes inesperados em meio à CSI quebram a sequência: o ``\\x1b`` é
    descartado (NÃO emitido como ESC genuíno — comportamento pré round 6
    causava cancel falso) e o resto vai pra próxima iteração.

    Args:
        buf: string acumulada de stdin (pode conter múltiplas teclas em
            sequência num único batch do kernel).

    Returns:
        ``(seqs, remainder)``. ``remainder`` pode incluir ``"\\x1b"`` solto
        no fim — só vira ESC após o timeout do caller.
    """
    seqs: List[str] = []
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch != "\x1b":
            # ASCII char comum (dígito, letra, etc.)
            seqs.append(ch)
            i += 1
            continue

        # ESC encontrado — pode ser ESC isolado OU prefixo de CSI/SS3.
        if i + 1 >= n:
            # Último byte do buffer — devolve como remainder pro caller
            # decidir (timeout → ESC genuíno; chegada de mais bytes →
            # próxima rodada de parse).
            return seqs, buf[i:]

        intro = buf[i + 1]
        if intro not in ("[", "O"):
            # ``\x1b`` seguido de algo que não é CSI/SS3 introducer.
            # Round 6 fix: NÃO interpretamos como ESC genuíno — o usuário
            # quase nunca digita ESC seguido de letra; mais provável é um
            # glitch do kernel ou sequência exótica. Descarta o ``\x1b``
            # e continua processando o byte como ASCII normal.
            i += 1  # pula o \x1b
            continue

        # Procura o terminador da CSI/SS3.
        # CSI (\x1b[): termina em byte final A-Z/a-z (0x40-0x7e).
        # SS3 (\x1bO): SEMPRE 3 bytes (\x1bO + 1 char).
        if intro == "O":
            if i + 2 >= n:
                # Sequência SS3 incompleta — guarda no remainder.
                return seqs, buf[i:]
            seqs.append(buf[i:i + 3])
            i += 3
            continue

        # CSI: varre até achar terminador.
        end = i + 2
        while end < n:
            b = buf[end]
            if 0x40 <= ord(b) <= 0x7e:
                break
            end += 1
        if end >= n:
            # CSI incompleta — guarda no remainder.
            return seqs, buf[i:]
        seqs.append(buf[i:end + 1])
        i = end + 1

    return seqs, ""


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

        renderer = SubAgentPanelRenderer(host_console, states, broadcast,
                                         real_stdout=sys.stdout)
        await renderer.run()    # bloqueia até todos os states virarem terminal
                                # ou ESC ser pressionado em vista compacta

    O orquestrador agenda :meth:`run` como ``asyncio.Task`` em paralelo aos
    runners — ver :class:`SubAgentOrchestrator`. Quando o usuário pressiona
    ESC na vista compacta, :attr:`cancelled` vira True e o orquestrador
    propaga o cancel aos runners pendentes.

    Args:
        host_console: console do streaming_renderer pai (CLI). Usado APENAS
            para detectar e suspender o Live do pai durante o painel.
        states: lista de estados (mutáveis pelos runners) a renderizar.
        broadcast: bus interno do orquestrador (subscreve pra acordar mais
            cedo em milestones — refresh ainda corre por timer).
        real_stdout: handle ao stdout *real* (capturado antes do redirect
            de ``sys.stdout`` feito pelo orquestrador). Quando ``None``, cai
            para ``sys.stdout`` corrente — modo headless / testes.
        refresh_hz: frequência mínima de redraw.
        enable_keyboard: ``False`` desabilita o watcher de teclado (testes).
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
        # Console dedicado: liga-se ao stdout REAL para que o painel apareça
        # mesmo enquanto ``sys.stdout`` está redirecionado. Quando o caller
        # não passa ``real_stdout``, reaproveitamos o host (modo headless).
        if real_stdout is not None:
            self._panel_console = Console(file=real_stdout, force_terminal=True)
        else:
            self._panel_console = host_console
        self._states = states
        self._broadcast = broadcast
        self._refresh_hz = max(1.0, float(refresh_hz))
        self._enable_keyboard = enable_keyboard
        # Foco: None = vista compacta; 1..N = ficha da frente N.
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
        """Vista compacta: 1 painel por sub-DEILE, com espaçamento.

        Fix do feedback #3 (issue #257 round 2): blank ``Text("")`` entre
        painéis dá respiro visual; sem isso ficam grudados (Rich Panel não
        adiciona margem própria).
        """
        items: List = [self._header_renderable(), Text("")]
        for i, st in enumerate(self._states):
            items.append(self._panel_for(st))
            # Blank line entre painéis (mas não depois do último).
            if i < len(self._states) - 1:
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
        header = self._header_renderable()
        ficha = self._ficha_for(st)
        execution = self._execution_block(st)
        hint = Text(
            "(ESC: voltar · ←/→: outra frente)",
            style="dim",
        )
        return Group(header, Text(""), ficha, Text(""), execution, Text(""), hint)

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

        # Title: status glyph + descrição + tempo. ``description`` é
        # LLM-supplied (vem do payload da tool) — pode conter ``[red]…[/]``
        # que o LLM gere literalmente. Sem escape, quebra o markup do painel.
        title = (
            f"[{style}]{glyph}[/{style}] "
            f"[bold]sub-DEILE #{st.task.index}[/bold] · "
            f"{_escape_markup(_truncate(st.task.description, 56))} "
            f"[dim]{elapsed}[/dim]"
        )

        # Corpo: até 3 últimas linhas de progresso + current_activity
        body_lines: List[str] = []
        recent = list(st.progress_lines)[-3:]
        for line in recent:
            body_lines.append(_escape_markup(_truncate(line, 70)))
        # Always show current_activity at the bottom if present and not duplicate
        if st.current_activity and (not recent or recent[-1] != st.current_activity):
            body_lines.append("… " + _escape_markup(_truncate(st.current_activity, 70)))
        if not body_lines:
            if status == "pending":
                body_lines.append("[dim]aguardando…[/dim]")
            else:
                body_lines.append("[dim](sem atividade ainda)[/dim]")

        # Final state collapses to a 1-line summary
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
        # prompt: até 6 linhas
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
        if lines:
            body = Text("\n".join(lines))
        else:
            body = Text("(sem atividade ainda)", style="dim")
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
        # Suspende o Live do pai (streaming_renderer) — Rich só permite um Live
        # ativo por console. Encapsulado em :func:`_safe_get_parent_live` porque
        # Rich não expõe API pública para "qual Live está ativo neste console"
        # e ``_live`` é privado/fragile (M7 — PR #295 review).
        prev_live = _safe_get_parent_live(self._host_console)
        if prev_live is not None:
            try:
                prev_live.stop()
            except Exception:
                logger.debug("Falha ao suspender Live do pai", exc_info=True)

        # Watcher de teclado em thread daemon (igual padrão do
        # cli._stream_with_esc_cancel). Sem TTY ou Windows-sem-termios, watcher
        # é no-op — painel ainda mostra status em tempo real, só não tem foco.
        # Reivindica stdin com exclusividade — o watcher do CLI principal
        # consulta esta flag e pausa enquanto estamos ativos (sem isso, ambos
        # competem pelos mesmos bytes e metade das teclas se perde).
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
            # ``sys.stdout = FileProxy(console)`` para que ``print()`` durante
            # a Live apareça acima da região. AQUI isso é tóxico: o orquestrador
            # já redirecionou sys.stdout para um buffer (suprimindo print() de
            # sub-DEILEs), e Live SOBRESCREVERIA esse redirect, mandando print()
            # do bash_tool diretamente para ``panel_console.file`` (= terminal
            # real) — gera leak visível (issue #257 round 5). Mantemos o
            # redirect do orquestrador intacto desabilitando o do Live.
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
                    # Sleep curto, acorda em milestones via _wake.
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
        except asyncio.CancelledError:
            # Cancelled pelo orquestrador (ex: timeout do outer). Aceita
            # silenciosamente — runners têm seu próprio cancel handler.
            raise
        except Exception:
            logger.exception("SubAgentPanelRenderer crashed")
        finally:
            kb_stop.set()
            if kb_thread is not None and kb_thread.is_alive():
                # Daemon thread — não bloqueia shutdown; 200ms é cortesia.
                kb_thread.join(timeout=0.2)
            # Devolve stdin pro CLI principal ANTES de tentar restaurar o
            # Live: se o restore falhar, ainda assim o flag fica limpo.
            if stdin_claimed:
                try:
                    release_stdin_for_panel()
                except Exception:
                    logger.debug("release_stdin_for_panel failed", exc_info=True)
            # Restaura o Live do pai. Se start() falhar (ex: o pai já fechou
            # seu Live no shutdown da CLI), tolera silenciosamente.
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
        """1 linha por sub-DEILE no fechamento — vai pra scrollback.

        Espaçamento (issue #257 round 5):
          * Linha em branco ANTES do header — separa do bloco anterior
            (``● dispatch_parallel_subagents(...) running…``).
          * SEM linha em branco entre header e items — adensar leitura,
            usuário pediu explicitamente.
        """
        rows: List = [
            Text(""),
            Text.from_markup(
                f"[bold cyan]🧩 Sub-DEILEs concluídos[/bold cyan] · "
                f"{_fmt_mmss(time.monotonic() - self._start_t)} total"
            ),
        ]
        for st in self._states:
            glyph = _STATUS_GLYPH.get(st.status, "•")
            style = _STATUS_STYLE.get(st.status, "white")
            tail = _files_tail(st.files_touched, head=5)
            elapsed = _fmt_mmss(st.elapsed_s)
            desc = _escape_markup(_truncate(st.task.description, 56))
            line = (
                f"  [{style}]{glyph}[/{style}] #{st.task.index} {desc} "
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

        # Snapshot atual + check se já estamos em cbreak (caso comum: CLI já
        # entrou em cbreak via :meth:`_stream_with_esc_cancel`). NÃO chamamos
        # setcbreak novamente se já estiver — evita o bug de double-restore
        # do CLI watcher acabar com termios cooked quando o painel sair antes.
        # O atexit em ``_stdin_owner`` é a rede de segurança absoluta para
        # Ctrl+C / exit abrupto.
        try:
            fd = sys.stdin.fileno()
        except Exception:
            return None  # sem fd, sem watcher
        try:
            current_attrs = termios.tcgetattr(fd)
            # lflag está no índice 3; ICANON ativo = modo cooked.
            already_cbreak = not bool(current_attrs[3] & termios.ICANON)
        except Exception:
            already_cbreak = True  # assume sim — não tentamos setar
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

        def _on_key(seq: str) -> None:
            """Aplica uma sequência de bytes capturada do stdin.

            ``seq`` pode ser:
              * 1 char ASCII (dígito, letra)
              * ``\\x1b`` solitário (ESC genuíno)
              * ``\\x1b[A``/``B``/``C``/``D`` (setas), ``\\x1bOX`` (F-keys)
              * Outros prefixos CSI ignorados.
            """
            n_states = len(self._states)
            if seq == "\x1b":
                # ESC: se em foco, volta à vista geral; senão, sinaliza saída.
                if self._focus is not None:
                    self._focus = None
                else:
                    self._cancel_requested = True
            elif len(seq) == 1 and seq.isdigit() and seq != "0":
                idx = int(seq)
                if 1 <= idx <= n_states:
                    self._focus = idx
            elif seq in ("\x1b[D", "\x1bOD"):  # left arrow (CSI ou SS3)
                if self._focus and self._focus > 1:
                    self._focus -= 1
            elif seq in ("\x1b[C", "\x1bOC"):  # right arrow
                if self._focus and self._focus < n_states:
                    self._focus += 1
            elif seq == "h":  # vim-style left (não conflita com prompt)
                if self._focus and self._focus > 1:
                    self._focus -= 1
            elif seq == "l":  # vim-style right
                if self._focus and self._focus < n_states:
                    self._focus += 1
            else:
                # Sequência não reconhecida — ignora silenciosamente em vez
                # de tratar como ESC (issue #257 round 2, fix #4).
                return
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                # Loop fechou — vai acordar no próximo tick mesmo assim.
                pass

        def _watch() -> None:
            """Lê stdin em rajada e despacha sequências completas.

            Round 6: substitui o parser byte-a-byte por leitura atômica via
            ``os.read(fd, 64)`` + parser de buffer. Vantagens:
              * Rajadas (3+ setas <50ms) chegam num único ``os.read`` —
                kernel já tem tudo no buffer e o select acorda só uma vez.
                ``parse_key_buffer`` separa cada seta em sua própria
                sequência sem usar timeouts ad-hoc.
              * ``read(1)`` que retornava ``""`` (signal) ou ``intro`` vazio
                não pode mais disparar ``_on_key("\\x1b")`` falso —
                ``parse_key_buffer`` descarta ``\\x1b`` órfão silenciosamente.
              * ESC genuíno só dispara após 200ms COM o ``\\x1b`` isolado
                no remainder do buffer (nenhuma outra tecla seguiu).
            """
            try:
                # ``fd`` já foi capturado no escopo externo (mesmo fd usado
                # pra detectar cbreak). Reutilizamos para evitar diferenças
                # de file descriptor entre threads.
                pending = ""        # bytes não-parseáveis (CSI incompleta)
                esc_deadline = 0.0  # > 0 quando ESC isolado está aguardando
                while not stop_event.is_set():
                    # Timeout do select: curto pra reagir a stop_event e
                    # apertado o suficiente pra confirmar ESC isolado.
                    timeout = 0.1
                    if esc_deadline > 0:
                        remaining = esc_deadline - time.monotonic()
                        if remaining <= 0:
                            # ESC isolado expirou — dispara cancel.
                            _on_key("\x1b")
                            pending = ""
                            esc_deadline = 0.0
                            continue
                        timeout = min(timeout, remaining)
                    r, _, _ = _select.select([sys.stdin], [], [], timeout)
                    if not r:
                        continue
                    # Leitura ATÔMICA — pega tudo o que o kernel tem no
                    # buffer agora (até 64 bytes, folgado p/ rajada manual).
                    try:
                        chunk = os.read(fd, _STDIN_READ_CHUNK)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        # EOF — stdin fechou.
                        break
                    try:
                        new_data = chunk.decode("utf-8", errors="replace")
                    except Exception:
                        new_data = ""
                    pending += new_data
                    seqs, pending = parse_key_buffer(pending)
                    for seq in seqs:
                        _on_key(seq)
                    # Se sobrou ``\x1b`` solto no remainder, arma o
                    # deadline pra confirmar ESC genuíno em 200ms.
                    if pending == "\x1b":
                        if esc_deadline == 0:
                            esc_deadline = time.monotonic() + _ESC_SEQUENCE_TIMEOUT_S
                    else:
                        esc_deadline = 0.0
            except Exception:
                logger.debug("keyboard watcher crashed", exc_info=True)
            finally:
                # Só restauramos se NÓS setamos cbreak (caso o painel rode
                # fora de um turno cbreak da CLI). Quando ``already_cbreak``,
                # quem setou (CLI ou outro) é responsável por restaurar.
                if we_set_cbreak and current_attrs is not None:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, current_attrs)
                    except Exception:
                        pass

        t = threading.Thread(target=_watch, daemon=True, name="subagent-kb-watcher")
        t.start()
        return t


def _safe_get_parent_live(console) -> Optional[object]:
    """Best-effort lookup do ``Live`` ativo no console pai.

    Rich 13.x não expõe API pública para "qual Live está ativo neste
    console" — armazena em ``Console._live`` (atributo privado). Sem suspender
    esse Live, abrir um segundo crasha com ``LiveError``. M7 (PR #295 review):
    encapsulamos o acesso aqui com try/except + comentário de fragilidade;
    se Rich um dia mudar o nome, retornamos ``None`` (fallback gracioso —
    o caller tolera ausência do Live pai).
    """
    try:
        return getattr(console, "_live", None)
    except Exception:
        logger.debug("Could not access Console._live (Rich API drift)", exc_info=True)
        return None


def _files_tail(files: list, *, head: int) -> str:
    """Format a "files touched" tail for the panel: " · a, b, c (+N)".

    Returns the formatted suffix (already including the leading separator)
    or an empty string when ``files`` is empty. Used by both the compact
    panel body and the final scrollback summary — keeps the truncation
    logic in one place.
    """
    if not files:
        return ""
    head_files = ", ".join(files[:head])
    tail = f" · {_escape_markup(head_files)}"
    if len(files) > head:
        tail += f" (+{len(files) - head})"
    return tail


def _truncate(text, limit: int) -> str:
    """Thin wrapper around :func:`deile.common.text_utils.truncate`.

    Kept as a module-local name because callers throughout this file (and
    one test in ``tests/ui/test_subagent_panel.py``) import it directly.
    Centralised implementation lives in ``common`` to share semantics with
    ``orchestration/subagents/runner._short``.
    """
    return truncate(text, limit)


def _escape_markup(text) -> str:
    """Escapa colchetes pra evitar que Rich interprete progress_lines (que
    podem conter ``[``/``]`` arbitrários vindos de tools) como markup.

    Crítico: ``progress_lines`` carrega texto não-confiável (output de bash,
    args de tools). Sem escape, um ``[red]`` no output do bash quebraria
    a renderização do Panel. Rich oferece ``escape`` em ``rich.markup``;
    usamos a versão pública.
    """
    if text is None:
        return ""
    return _rich_escape(str(text))


__all__ = ["SubAgentPanelRenderer", "parse_key_buffer"]
