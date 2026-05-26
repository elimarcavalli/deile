"""Progressive transcript renderer for the agent's streaming turns.

Consumes ``UnifiedStreamEvent`` objects and renders them to a ``rich.Console``
as they arrive — text deltas accumulate inline, tool calls show up as
status-tagged blocks that flip from "running" → "✓/✗" once the
``TOOL_RESULT`` event lands.

Architecture (Markdown-aware streaming):

1. **Accumulator pattern** — every ``TEXT_DELTA`` is appended to a per-source
   ``_TextBlock``. The renderer never tries to parse the *current* delta in
   isolation; it always re-renders the *accumulated* text. This is what
   ``rich.markdown.Markdown`` requires — it consumes a complete (possibly
   transitional) Markdown document, not a token stream. Partial fences
   (``"```py\n..."`` without a closing ``"```"``) and split inline runs
   (``"**tex"`` → ``"to**"``) become well-formed Markdown the moment the
   closing token arrives, and the next ``Live.update`` redraws the diff.
2. **Live region with virtual-DOM diffing** — ``rich.live.Live`` uses ANSI
   cursor-positioning to repaint only the changed lines, avoiding the
   "wall of repeated text" effect of naively printing each frame.
3. **Throttled refresh** — ``refresh_per_second`` (default 12 Hz) decouples
   network speed from terminal redraw speed. The network coroutine pushes
   into the block list as fast as it likes; ``Live`` flushes at the chosen
   frame rate.
4. **Legacy fallback** — for terminals where ``Live`` is unsafe (true
   legacy Windows conhost without ANSI), we accumulate the same way and
   flush rendered Markdown in batches, throttled by the same parameter.
   Markdown is still produced; only the in-place refresh is sacrificed.

Decoupled from ``ConsoleUIManager`` so it can be tested with a captured
console (``Console(file=StringIO())``) without spinning up a terminal.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from deile.common.tool_args import TOOL_PRIMARY_ARG_KEYS
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.ui.markdown_table import DeileMarkdown as Markdown
from deile.ui.markdown_table import safe_streaming_split

logger = logging.getLogger(__name__)

_VALIDATION_GATE_TITLE = (
    "[yellow]⚠ resposta corrigida — a anterior afirmou conclusão sem validar[/yellow]"
)

# Tools que escrevem direto no stdout durante a execução. Para elas,
# o cabeçalho "● Bash(...)" precisa ir para a scrollback assim que
# os args chegam, antes da execução, para evitar colisão com a Live region.
#
# ``dispatch_parallel_subagents`` (issue #257) também entra aqui: durante a
# execução ele abre o próprio Rich Live (SubAgentPanelRenderer) e suspende o
# Live do streaming_renderer; o cabeçalho precisa ir pra scrollback ANTES
# para que o painel multipanel apareça logo abaixo, sem colidir com a tag
# "● dispatch_parallel_subagents(...)".
_DIRECT_PRINT_TOOLS: frozenset = frozenset({
    "bash_execute",
    "dispatch_parallel_subagents",
})

# Mapeamento opcional de nome interno → nome amigável exibido.
_TOOL_DISPLAY_NAME: Dict[str, str] = {
    "bash_execute": "Bash",
    "python_execute": "Python",
}

# Para tools de comando primário (uma string única dominante), mostramos
# o valor cru em vez do par "chave='valor'". Mapeia tool → arg principal.
# Alias preservado para callers internos; a fonte única de verdade vive em
# ``deile.common.tool_args`` (compartilhada com o runner de sub-agentes).
_TOOL_PRIMARY_ARG: Dict[str, str] = TOOL_PRIMARY_ARG_KEYS

# Tools com renderização de args customizada. Quando o nome aparece aqui,
# ``_render_args_inline`` delega à função associada em vez de usar o
# formatador genérico ``chave=valor`` — necessário para tools como
# ``edit_file`` cujo argumento ``patches`` é uma lista de dicts que
# renderiza horrivelmente via ``str(list)``.
def _format_edit_file_args(args: Dict[str, Any]) -> str:
    """``edit_file(file_path, N patches)`` em vez de Python repr da lista."""
    path = args.get("file_path") or args.get("path") or ""
    if isinstance(path, str) and len(path) > 60:
        path = "…" + path[-57:]
    patches = args.get("patches")
    if isinstance(patches, list):
        count = len(patches)
        suffix = f"{count} patch" if count == 1 else f"{count} patches"
        if path:
            return f"{path}, {suffix}"
        return suffix
    # Fallback for legacy {old_string, new_string} shape (single patch).
    if "old_string" in args or "new_string" in args:
        return f"{path}, 1 patch" if path else "1 patch"
    return str(path) if path else ""


_TOOL_ARG_FORMATTERS: Dict[str, Any] = {
    "edit_file": _format_edit_file_args,
}


@dataclass
class _ToolBlock:
    tool_call_id: str
    tool_name: str
    args: Optional[Dict[str, Any]] = None
    status: str = "running"  # running | success | error
    summary: Optional[str] = None
    iteration: Optional[int] = None
    # Para tools que escrevem direto no stdout (ex.: bash_execute),
    # comprometemos o cabeçalho na scrollback assim que os args chegam,
    # antes da execução. Sem isso, prints da tool sobrescrevem a Live
    # region e o usuário pode não ver qual comando rodou.
    head_committed: bool = False  # cabeçalho já impresso na scrollback
    summary_committed: bool = False  # linha "⎿ summary" já impressa


@dataclass
class _TextBlock:
    text: str = ""
    source: Optional[str] = None  # e.g. "validation_gate"


@dataclass
class _RenderableBlock:
    """A pre-built Rich renderable carried verbatim through the stream.

    Slash commands like ``/model list`` return ``rich.table.Table`` objects
    that must be printed by Rich itself so its width-aware column-layout
    algorithm runs at the actual terminal width. Flattening them to text
    and re-rendering as Markdown shatters the layout.
    """

    renderable: Any = None


@dataclass
class _StageBlock:
    """Transient progress indicator shown at the tail of the block list.

    A StageBlock is always the LAST block — it's appended (or its label is
    updated in place) when a STAGE event arrives, and removed the moment a
    real content event (TEXT_DELTA / TOOL_USE_END / TOOL_RESULT / USAGE_FINAL
    / ERROR) lands. This way the user always sees what the agent is doing
    during otherwise silent gaps (next-iteration round-trip, tool execution,
    pre-stream pipeline) without polluting the final transcript.
    """

    text: str = ""
    progress_current: Optional[int] = None
    progress_total: Optional[int] = None
    progress_label: Optional[str] = None


@dataclass
class RenderResult:
    """Aggregated transcript captured by the renderer.

    Useful for tests and for callers that want the same text the user saw
    without re-parsing the events.
    """

    full_text: str = ""
    tool_invocations: int = 0
    tool_failures: int = 0
    error_message: Optional[str] = None


class StreamingRenderer:
    """Render an event stream progressively to a Rich Console.

    Args:
        console: target console; pass ``Console(file=StringIO())`` to capture
            output for tests.
        legacy_windows: when ``True``, fall back to append-only rendering
            (no in-place ``Live`` refresh) — useful on terminals that
            mishandle ANSI cursor moves.
        markdown: render assistant text as Markdown when ``True``; emit plain
            text otherwise. Defaults to ``True``.
        refresh_per_second: ``rich.live.Live`` refresh rate.
    """

    def __init__(
        self,
        console: Console,
        legacy_windows: bool = False,
        markdown: bool = True,
        refresh_per_second: float = 12.0,
    ) -> None:
        self._console = console
        # Treat the console as legacy ONLY if Rich itself reports it as legacy
        # (true cmd.exe-without-ANSI scenarios) or the caller explicitly opts
        # in. Previously this also fired when ``legacy_windows`` was True for
        # any reason — including ConsoleUIManager's blanket Windows-fallback
        # configuration — which silently disabled Markdown rendering on
        # macOS/Linux/modern Windows. The caller now decides explicitly.
        self._legacy = bool(legacy_windows) or bool(getattr(console, "legacy_windows", False))
        self._markdown = markdown
        # Clamp to a sane range. 12 Hz is the sweet spot reported by Rich's
        # docs for streaming — high enough to feel real-time, low enough not
        # to saturate stdout on a slow terminal.
        self._refresh_hz = max(1.0, float(refresh_per_second))
        # Pre-compute the minimum interval between flushes for the legacy
        # path (no Live → we throttle manually).
        self._legacy_flush_interval = 1.0 / self._refresh_hz
        # Anti-flicker: skip STAGE label changes within 3 seconds to prevent
        # the spinner text from blurring. Exception: real phase change.
        self._last_stage_update: float = 0.0
        self._last_stage_label: Optional[str] = None
        self._min_stage_interval: float = 3.0

    async def render(self, event_stream: AsyncIterator[UnifiedStreamEvent]) -> RenderResult:
        """Drive the stream to completion and return a summary."""
        result = RenderResult()
        blocks: List[Any] = []
        # Reset anti-flicker state for fresh turn.
        self._last_stage_update = 0.0
        self._last_stage_label = None

        if self._legacy:
            await self._render_legacy(event_stream, blocks, result)
        else:
            await self._render_live(event_stream, blocks, result)

        result.full_text = "".join(
            b.text for b in blocks if isinstance(b, _TextBlock)
        )
        result.tool_invocations = sum(1 for b in blocks if isinstance(b, _ToolBlock))
        result.tool_failures = sum(
            1 for b in blocks if isinstance(b, _ToolBlock) and b.status == "error"
        )
        return result

    # ------------------------------------------------------------------
    # Live (in-place refresh) — default path
    # ------------------------------------------------------------------

    # Edge-event types that force an immediate refresh: any structural
    # change (tool block appears, status flips, error, end-of-turn) MUST
    # be visible to the user the instant it happens, regardless of the
    # rate-limit window. Mid-stream TEXT_DELTAs are batched at refresh_hz.
    _EDGE_EVENT_TYPES = frozenset({
        StreamEventType.TOOL_USE_START,
        StreamEventType.TOOL_USE_END,
        StreamEventType.TOOL_RESULT,
        StreamEventType.USAGE_FINAL,
        StreamEventType.ERROR,
    })

    async def _render_live(
        self,
        event_stream: AsyncIterator[UnifiedStreamEvent],
        blocks: List[Any],
        result: RenderResult,
    ) -> None:
        # Per-block Live, scrollback for completed blocks. Rationale:
        #   * A single Live region growing across the entire stream becomes
        #     taller than the terminal. Rich's cursor-positioning logic
        #     assumes the rendered region fits, so when content overflows
        #     the user sees text "jump" — earlier blocks appearing AFTER
        #     later ones because the Live redraw scrolls unpredictably.
        #   * Solution: only the ACTIVE block (the one currently being
        #     written to) lives in the Live region. As soon as a block
        #     becomes "done" (a newer block has been started after it AND
        #     it isn't a still-running tool), it is committed to the
        #     terminal scrollback via ``console.print``. Rich Live cleanly
        #     prints above its own region, so the user sees blocks flow
        #     downward in the exact order events arrived.
        #   * Live stays small (~1 block) → cursor positioning is always
        #     correct → no jump-to-top glitches.
        #   * ``auto_refresh=False`` + manual ``live.refresh()`` per event
        #     gives deterministic, race-free painting. We refresh on EVERY
        #     event so the user sees text/tool blocks appear the instant
        #     the provider yields them — no rate-limit-induced staleness.
        self._console.print("\n[bold #4285F4]Deile >[/]")

        usage_footer: Optional[str] = None
        # blocks[0..committed_count-1] are already in scrollback; Live only
        # renders blocks[committed_count:] (typically just the last block).
        committed_count = 0

        # Thinking indicator: a small animated spinner shown in the Live
        # region while we wait for the FIRST event. The provider's
        # time-to-first-token (plus any pre-stream agent work — proactive
        # tools, parsing, workflow checks) can take several seconds, and
        # without feedback the user thinks the agent is hung. The spinner
        # is cancelled on the first event, so it never overlaps real
        # content.
        from .spinner import BRAILLE_SPINNER_FRAMES as _SPINNER_FRAMES

        # Animation tick — runs for the entire turn. As long as the tail
        # of ``blocks`` is a _StageBlock (i.e., we're in a "silent wait"),
        # we refresh the Live region so the spinner frame visibly rotates.
        # When real content arrives, the _StageBlock is popped by
        # ``_apply_event`` and this loop refreshes nothing extra.
        turn_done = [False]
        spinner_frame_idx = [0]

        async def _thinking_spinner(live_obj: Live) -> None:
            # Anti-flicker (Bug A): durante a execução de tools que abrem o
            # próprio Rich Live (ex.: ``dispatch_parallel_subagents`` via
            # :class:`SubAgentPanelRenderer`), o Live pai é suspenso
            # (``prev_live.stop()``); tentar ``refresh()`` num Live parado
            # é no-op interno do Rich, mas qualquer ciclo extra colide com
            # o repaint do painel filho quando o pai é restaurado. Guarda
            # via ``is_started`` (API pública do Rich) para pular o tick
            # nesse caso. Também subimos o sleep de 100ms → 250ms (4 Hz):
            # o spinner ainda parece animado mas reduz refresh em 60%.
            try:
                while not turn_done[0]:
                    # ``is_started`` é False quando o Live foi parado por um
                    # consumidor que precisava de exclusividade no console
                    # (ex.: subagent_panel). Skip o tick para evitar flicker
                    # ao retomar.
                    started = getattr(live_obj, "is_started", True)
                    if started and blocks and isinstance(blocks[-1], _StageBlock):
                        spinner_frame_idx[0] = (spinner_frame_idx[0] + 1) % len(_SPINNER_FRAMES)
                        live_obj.update(self._compose(
                            blocks[committed_count:],
                            spinner_frame=_SPINNER_FRAMES[spinner_frame_idx[0]],
                        ))
                        live_obj.refresh()
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                return

        # Seed: open the turn with a generic stage so the spinner appears
        # immediately, before the agent has a chance to emit its first STAGE.
        blocks.append(_StageBlock(text="Pensando..."))

        # Separador visual antes de iniciar o stream da resposta — demarca
        # o turno do assistente e facilita a leitura em sessões longas.
        # ``rule()`` consulta ``console.width`` corrente, então adapta ao
        # tamanho atual do terminal a cada renderização.
        from deile.ui.dynamic_render import turn_separator
        turn_separator(self._console)

        with Live(
            self._compose(blocks),
            console=self._console,
            refresh_per_second=self._refresh_hz,
            transient=False,
            auto_refresh=False,
        ) as live:
            spinner_task = asyncio.create_task(_thinking_spinner(live))
            try:
                async for event in event_stream:
                    # Bug B mitigation: o loop de render NUNCA pode quebrar
                    # silenciosamente. Se ``_apply_event``, ``_compose`` ou um
                    # ``Text.from_markup`` no caminho levantar (ex.: ``MarkupError``
                    # por ``[`` literal em args de tool, summary de tool com
                    # markup malformado, ou Markdown com sintaxe quebrada), o
                    # ``async for`` continua consumindo eventos mas a Live
                    # region NÃO REPINTA — usuário vê o stream "travar
                    # silenciosamente" até o turn terminar (quando o `finally`
                    # ou um caller acima força um repaint final).
                    #
                    # A defesa aqui: cada *etapa* do laço é isolada num
                    # try/except que LOGA com contexto e segue. Histórico
                    # ainda é registrado normalmente (esse caminho é do
                    # core/agent.py, não daqui). O usuário pode perder um
                    # frame intermediário, mas o turno completa e a Live
                    # mostra todo o conteúdo no próximo evento bem-formado.
                    try:
                        self._apply_event(event, blocks, result)
                    except Exception as exc:
                        logger.warning(
                            "streaming_renderer: _apply_event falhou em %s — pulando frame",
                            getattr(event, "type", "?"),
                            exc_info=True,
                        )
                        # Não retornamos — drenar o stream é essencial; o
                        # próximo evento pode reconciliar o estado.
                        continue
                    if event.type is StreamEventType.USAGE_FINAL and event.usage:
                        u = event.usage
                        usage_footer = (
                            f"\n[dim]:hourglass: {u.input_tokens} in / "
                            f"{u.output_tokens} out"
                            + (f" • ${u.cost_usd:.4f}" if u.cost_usd else "")
                            + (f" • {u.model}" if u.model else "")
                            + "[/dim]"
                        )

                    # Determine the first "active" block (the one currently
                    # being modified). Everything before it can be committed.
                    try:
                        active_idx = self._first_active_block_idx(blocks, committed_count)
                        if active_idx > committed_count:
                            # First, shrink the Live region so the to-be-committed
                            # blocks aren't rendered both in Live AND in scrollback
                            # for a single frame.
                            live.update(self._compose(blocks[active_idx:]))
                            live.refresh()
                            # Now flush the completed blocks to scrollback.
                            # Each committed block is followed by a blank line so
                            # tool blocks and text blocks never appear glued
                            # together (matches the spacer rule in _compose).
                            for i in range(committed_count, active_idx):
                                try:
                                    renderable = self._render_single_block(blocks[i])
                                except Exception:
                                    logger.warning(
                                        "streaming_renderer: render do bloco %d falhou",
                                        i, exc_info=True,
                                    )
                                    renderable = None
                                if renderable is not None:
                                    try:
                                        self._console.print(renderable)
                                        self._console.print()
                                    except Exception:
                                        logger.warning(
                                            "streaming_renderer: console.print falhou no bloco %d",
                                            i, exc_info=True,
                                        )
                            committed_count = active_idx
                    except Exception:
                        logger.warning(
                            "streaming_renderer: commit-to-scrollback falhou — segue",
                            exc_info=True,
                        )

                    # AGORA (após texto/blocos precedentes irem para scrollback)
                    # comprometemos cabeçalho/summary de tools direct-print. Isso
                    # garante a ordem visual correta:
                    #   <texto modelo> → ● Bash(...) → <stdout da bash> → ⎿ summary
                    # Sem este reordenamento, o cabeçalho da bash sairia ANTES
                    # do texto precedente do modelo (todo o texto ficava preso
                    # na Live region até o final do turno).
                    try:
                        self._commit_direct_print_tools(blocks)
                    except Exception:
                        logger.warning(
                            "streaming_renderer: commit direct-print falhou — segue",
                            exc_info=True,
                        )

                    # Refresh on every event. With auto_refresh=False this is
                    # the ONLY way pixels reach the terminal — and there's no
                    # background thread to race against, so unconditional
                    # refresh is safe and gives the most responsive feel.
                    try:
                        live.update(self._compose(blocks[committed_count:]))
                        live.refresh()
                    except Exception:
                        # Live.update/refresh ou _compose pode levantar
                        # (MarkupError, ConsoleError, etc.). Loga e segue —
                        # o próximo evento tem outra chance de repintar.
                        logger.warning(
                            "streaming_renderer: live.update/refresh falhou — frame perdido",
                            exc_info=True,
                        )
                # Final flush — pass force_complete=True so any trailing
                # in-progress table (no closing blank line) commits as a
                # real Markdown table in the scrollback, instead of the
                # dim raw-pipes placeholder used during streaming.
                live.update(self._compose(blocks[committed_count:], force_complete=True))
                live.refresh()
            except KeyboardInterrupt:
                live.update(self._compose(blocks[committed_count:], footer="[yellow]\n(interrupted)[/yellow]"))
                live.refresh()
                raise
            finally:
                # Mark the turn done first so the animation loop exits
                # cleanly on its next tick; then cancel as a safety net.
                turn_done[0] = True
                if not spinner_task.done():
                    spinner_task.cancel()
                    try:
                        await spinner_task
                    except (asyncio.CancelledError, Exception):
                        pass
                # If a stage was still pending at shutdown, drop it so it
                # doesn't leak into scrollback.
                while blocks and isinstance(blocks[-1], _StageBlock):
                    blocks.pop()

        if usage_footer:
            self._console.print(usage_footer)

    def _first_active_block_idx(self, blocks: List[Any], committed_count: int) -> int:
        """Index of the first block that is still being modified.

        A block is *active* if events can still mutate it:
          * a ``_ToolBlock`` whose status is still ``running`` is awaiting
            ``TOOL_RESULT`` and may change;
          * the LAST block in the list is, by definition, the one we are
            currently appending to (text deltas, tool result summary, etc.).

        Blocks strictly before the active one are stable and safe to commit
        to scrollback.
        """
        if not blocks:
            return committed_count
        for i in range(committed_count, len(blocks) - 1):
            block = blocks[i]
            if isinstance(block, _ToolBlock) and block.status == "running":
                # Tools direct-print imprimem o cabeçalho na scrollback
                # antes de executar; não devem segurar a Live region.
                if block.head_committed:
                    continue
                return i
            # _StageBlock is always transient — if one ends up not at the
            # tail (shouldn't normally happen), still treat it as active so
            # we never commit it to scrollback.
            if isinstance(block, _StageBlock):
                return i
        return len(blocks) - 1

    def _commit_direct_print_tools(self, blocks: List[Any]) -> None:
        """Imprime cabeçalho/summary de tools direct-print direto na scrollback.

        Bash escreve em stdout via ``print()`` durante a execução, o que colide
        com a Live region. Para garantir que o usuário sempre veja o comando
        executado (antes do output) e o resumo (após), comprometemos esses
        elementos via ``console.print`` — o Rich Live lida com prints acima
        da região automaticamente, preservando a ordem visual.

        Chamado DEPOIS do active_idx commit do laço principal, garantindo que
        qualquer texto/bloco precedente já está na scrollback antes do
        cabeçalho — sem isso, o `● Bash(...)` imprimia ANTES do texto do
        modelo que o introduzia.
        """
        for b in blocks:
            if not isinstance(b, _ToolBlock):
                continue
            if b.tool_name not in _DIRECT_PRINT_TOOLS:
                continue
            # Cabeçalho: imprime assim que os args estão disponíveis (TOOL_USE_END).
            if not b.head_committed and b.args is not None:
                self._console.print(Text.from_markup(self._tool_head_markup(b)))
                b.head_committed = True
            # Summary: imprime assim que o status sai de "running" (TOOL_RESULT).
            if b.head_committed and not b.summary_committed and b.status != "running":
                if b.summary:
                    self._console.print(Text.from_markup(
                        self._tool_summary_markup(b).lstrip("\n")
                    ))
                # Blank line após o summary p/ separar visualmente do próximo
                # bloco (texto do LLM, próxima tool, etc.) — alinhado com o
                # padrão dos blocos não-direct-print. Issue #257 round 5
                # (usuário pediu espaçamento entre ``⎿`` e o próximo conteúdo).
                self._console.print()
                b.summary_committed = True

    def _render_single_block(self, block: Any) -> Optional[Any]:
        """Render a single block as a Rich renderable for static print."""
        # Stage blocks are never committed to scrollback.
        if isinstance(block, _StageBlock):
            return None
        if isinstance(block, _RenderableBlock):
            return block.renderable
        if isinstance(block, _TextBlock):
            if not block.text:
                return None
            if block.source == "validation_gate":
                return Panel(
                    Text(block.text, style="yellow"),
                    title=_VALIDATION_GATE_TITLE,
                    border_style="yellow",
                )
            if block.source == "error":
                return Text.from_markup(block.text)
            if self._markdown:
                try:
                    return Markdown(block.text)
                except Exception:
                    return Text(block.text)
            return Text(block.text)
        if isinstance(block, _ToolBlock):
            # Tools direct-print já tiveram cabeçalho impresso eagermente na
            # scrollback (e o summary será impresso quando o resultado chegar).
            # Re-renderizar aqui duplicaria o cabeçalho.
            if block.head_committed:
                return None
            return self._tool_renderable(block)
        return None

    # ------------------------------------------------------------------
    # Legacy (append-only, no in-place refresh) — still Markdown-aware
    # ------------------------------------------------------------------
    #
    # When ``Live`` is unsafe (true legacy Windows conhost without ANSI), we
    # cannot do diff-based redraws. But we MUST still render Markdown — the
    # previous implementation printed raw deltas, which surfaced the source
    # bug the user reported (raw ``**bold**`` and ``# headings`` in the
    # terminal). Strategy here:
    #
    #   * accumulate text into ``_TextBlock``s exactly like the live path;
    #   * tool start/result events print immediately (single-line, low cost);
    #   * on a USAGE_FINAL / ERROR / end-of-stream boundary, flush the
    #     accumulated text as a single ``Markdown`` render — this is the
    #     same "re-render the full accumulated buffer" approach Rich uses
    #     internally and it correctly handles partial Markdown that was
    #     completed mid-stream;
    #   * between boundaries we ALSO flush opportunistically when the
    #     accumulator grows past a soft threshold AND enough time has passed
    #     since the last flush (throttled at ``refresh_per_second``), so the
    #     user sees progressive output rather than one big wall at the end.
    #
    # The result is: Markdown is honored even on the legacy path; the only
    # capability we lose vs. the Live path is in-place diffing of text
    # blocks (the legacy path appends each batch).

    _LEGACY_FLUSH_CHAR_THRESHOLD = 80

    async def _render_legacy(
        self,
        event_stream: AsyncIterator[UnifiedStreamEvent],
        blocks: List[Any],
        result: RenderResult,
    ) -> None:
        self._console.print("\nDeile >")
        last_flush = time.monotonic()
        chars_since_flush = 0
        rendered_text_len_per_block: Dict[int, int] = {}

        async for event in event_stream:
            # STAGE events: the legacy path can't repaint a spinner in place,
            # so we print each stage as its own dim line. The user still gets
            # a step-by-step trail of what the agent is doing pre-stream.
            if event.type is StreamEventType.STAGE:
                if event.stage:
                    self._console.print(f"[dim]⠋ {event.stage}…[/dim]")
                continue

            self._apply_event(event, blocks, result)

            if event.type is StreamEventType.TEXT_DELTA and event.text:
                chars_since_flush += len(event.text)
                now = time.monotonic()
                if (
                    chars_since_flush >= self._LEGACY_FLUSH_CHAR_THRESHOLD
                    and (now - last_flush) >= self._legacy_flush_interval
                ):
                    self._legacy_flush_text(blocks, rendered_text_len_per_block)
                    last_flush = now
                    chars_since_flush = 0

            elif event.type is StreamEventType.TOOL_USE_END:
                # Flush any pending text before the tool block prints.
                self._legacy_flush_text(blocks, rendered_text_len_per_block, final=True)
                display_name = _TOOL_DISPLAY_NAME.get(
                    event.tool_name or "", event.tool_name or "<tool>"
                )
                args_preview = self._render_args_inline(
                    event.tool_name or "", event.arguments
                )
                self._console.print(
                    f"\n[yellow]●[/yellow] [bold]{display_name}[/bold]"
                    f"({args_preview}) [dim]running…[/dim]"
                )
                last_flush = time.monotonic()
                chars_since_flush = 0

            elif event.type is StreamEventType.RICH_RENDERABLE:
                # Flush any pending text before the renderable prints so
                # the visual order matches the event order.
                self._legacy_flush_text(blocks, rendered_text_len_per_block, final=True)
                if event.renderable is not None:
                    self._console.print(event.renderable)

            elif event.type is StreamEventType.TOOL_RESULT:
                marker_color = "red" if event.tool_status == "error" else "green"
                summary = self._safe_markup(event.tool_result_summary or "")
                self._console.print(
                    f"  [{marker_color}]⎿[/{marker_color}] [dim]{summary}[/dim]"
                )

            elif event.type is StreamEventType.USAGE_FINAL and event.usage:
                # Final flush of any unrendered tail text BEFORE the usage line.
                self._legacy_flush_text(blocks, rendered_text_len_per_block, final=True)
                u = event.usage
                self._console.print(
                    f"\n[dim]:hourglass: {u.input_tokens} in / {u.output_tokens} out"
                    + (f" • ${u.cost_usd:.4f}" if u.cost_usd else "")
                    + (f" • {u.model}" if u.model else "")
                    + "[/dim]"
                )

            elif event.type is StreamEventType.ERROR:
                self._legacy_flush_text(blocks, rendered_text_len_per_block, final=True)

        # Stream ended — flush any remaining text.
        self._legacy_flush_text(blocks, rendered_text_len_per_block, final=True)

    def _legacy_flush_text(
        self,
        blocks: List[Any],
        rendered_lengths: Dict[int, int],
        *,
        final: bool = False,
    ) -> None:
        """Render the *new* portion of every ``_TextBlock`` accumulated.

        We track per-block "already-rendered length" so we don't re-print the
        same prefix on every flush. We pass the *new* slice to ``Markdown``
        when it is reasonably self-contained; on a non-final flush we hold
        back unfinished trailing fences/asterisks to avoid corrupting them.
        On the final flush we render whatever is left, even if transitional.
        """
        for idx, block in enumerate(blocks):
            if not isinstance(block, _TextBlock):
                continue
            already = rendered_lengths.get(idx, 0)
            new_text = block.text[already:]
            if not new_text:
                continue

            if not final:
                # Hold back an obviously-unclosed trailing token so partial
                # markup doesn't print ugly. Only matters between flushes;
                # the final flush always renders everything.
                safe_until = self._safe_markdown_cutoff(new_text)
                if safe_until == 0:
                    continue
                new_text = new_text[:safe_until]

            renderable = self._render_text_block(block, new_text)
            if renderable is not None:
                self._console.print(renderable)
            rendered_lengths[idx] = already + len(new_text)

    @staticmethod
    def _safe_markdown_cutoff(text: str) -> int:
        """Return the longest prefix of ``text`` ending at a safe boundary.

        Used by the legacy progressive flush so we don't print half of a
        ``**bold**`` run, half of an open code fence, or a half-built GFM
        table between flushes. The Live path doesn't need the inline-markup
        guards because ``Markdown`` re-parses the full accumulated buffer
        every frame, but it DOES use ``safe_streaming_split`` for tables
        for the same jitter-suppression reason.
        """
        # If we're inside an open fenced code block (odd count of triple
        # backticks), wait for the close.
        if text.count("```") % 2 == 1:
            return 0
        # If the trailing tail looks like an unclosed inline run, trim back
        # to the last newline (sentence boundary is safer than mid-run).
        tail = text.rstrip()
        for token in ("**", "__", "`", "[", "*", "_"):
            # Odd count of token + tail ends inside it → cut at last newline.
            if text.count(token) % 2 == 1 and tail.endswith(token[0]):
                last_nl = text.rfind("\n")
                return max(last_nl + 1, 0)
        # Open-table guard: if the trailing block is an unfinished GFM
        # table (no closing blank line yet), stop at its start so we don't
        # print rows that will reflow once the table closes.
        stable_prefix, transient_tail = safe_streaming_split(text)
        if transient_tail:
            return len(stable_prefix)
        return len(text)

    def _render_text_block(self, block: "_TextBlock", text: str):
        """Build the Rich renderable for a chunk of text from ``block``."""
        if not text:
            return None
        if block.source == "validation_gate":
            return Panel(
                Text(text, style="yellow"),
                title=_VALIDATION_GATE_TITLE,
                border_style="yellow",
            )
        if block.source == "error":
            return Text.from_markup(text)
        if self._markdown:
            try:
                return Markdown(text)
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("Markdown render failed, falling back to text: %s", exc)
                return Text(text)
        return Text(text)

    # ------------------------------------------------------------------
    # Event → block-list mutation
    # ------------------------------------------------------------------

    def _apply_event(
        self,
        event: UnifiedStreamEvent,
        blocks: List[Any],
        result: RenderResult,
    ) -> None:
        # STAGE events update the trailing transient progress indicator.
        # They never produce permanent content — if a _StageBlock is already
        # at the tail, mutate its label in place; otherwise append one.
        # Anti-flicker: skip label changes within MIN_STAGE_INTERVAL unless
        # the label itself is identical (idempotent update).
        if event.type is StreamEventType.STAGE:
            label = event.stage or ""
            now = time.monotonic()
            if label != self._last_stage_label:
                if now - self._last_stage_update < self._min_stage_interval:
                    return
                self._last_stage_label = label
                self._last_stage_update = now
            if blocks and isinstance(blocks[-1], _StageBlock):
                blocks[-1].text = label
                blocks[-1].progress_current = None
                blocks[-1].progress_total = None
            else:
                blocks.append(_StageBlock(text=label))
            return

        # PROGRESS events carry structured counters for long-running operations.
        # They update the trailing _StageBlock (creating one if needed) and
        # render the counter inline: "⠋ label (current/total)…"
        if event.type is StreamEventType.PROGRESS:
            label = event.progress_label or event.stage or "Processando"
            now = time.monotonic()
            if label != self._last_stage_label:
                if now - self._last_stage_update < self._min_stage_interval:
                    return
                self._last_stage_label = label
                self._last_stage_update = now
            if blocks and isinstance(blocks[-1], _StageBlock):
                blocks[-1].text = label
                blocks[-1].progress_current = event.progress_current
                blocks[-1].progress_total = event.progress_total
                blocks[-1].progress_label = event.progress_label
            else:
                blocks.append(_StageBlock(
                    text=label,
                    progress_current=event.progress_current,
                    progress_total=event.progress_total,
                    progress_label=event.progress_label,
                ))
            return

        # Any non-STAGE/PROGRESS event means real content is arriving — drop
        # the transient stage indicator before processing so the new content
        # takes its place.
        while blocks and isinstance(blocks[-1], _StageBlock):
            blocks.pop()

        if event.type is StreamEventType.TEXT_DELTA:
            if not event.text:
                return
            # If the most recent block is text and same source, append; else open new.
            if blocks and isinstance(blocks[-1], _TextBlock) and blocks[-1].source == event.source:
                blocks[-1].text += event.text
            else:
                blocks.append(_TextBlock(text=event.text, source=event.source))
        elif event.type is StreamEventType.RICH_RENDERABLE:
            if event.renderable is not None:
                blocks.append(_RenderableBlock(renderable=event.renderable))
        elif event.type is StreamEventType.TOOL_USE_START:
            blocks.append(
                _ToolBlock(
                    tool_call_id=event.tool_call_id or "",
                    tool_name=event.tool_name or "<tool>",
                    iteration=event.iteration,
                )
            )
        elif event.type is StreamEventType.TOOL_USE_END:
            block = self._find_tool_block(blocks, event.tool_call_id)
            if block is not None:
                block.args = event.arguments
            else:
                # Provider emitted END without START — synthesize a block.
                blocks.append(
                    _ToolBlock(
                        tool_call_id=event.tool_call_id or "",
                        tool_name=event.tool_name or "<tool>",
                        args=event.arguments,
                        iteration=event.iteration,
                    )
                )
        elif event.type is StreamEventType.TOOL_RESULT:
            block = self._find_tool_block(blocks, event.tool_call_id)
            if block is not None:
                block.status = event.tool_status or "success"
                block.summary = event.tool_result_summary
            else:
                blocks.append(
                    _ToolBlock(
                        tool_call_id=event.tool_call_id or "",
                        tool_name=event.tool_name or "<tool>",
                        status=event.tool_status or "success",
                        summary=event.tool_result_summary,
                        iteration=event.iteration,
                    )
                )
        elif event.type is StreamEventType.ERROR:
            result.error_message = self._error_message(event.error_envelope)
            display_msg = result.error_message
            if isinstance(event.error_envelope, dict) and event.error_envelope.get("budget_exceeded"):
                display_msg += "\nUse /model budget to view limits, or wait for the next window."
            # Escapa colchetes do display_msg — caso a mensagem do provider
            # contenha ``[`` literal, ``Text.from_markup`` (usado em _compose
            # via source="error") levantaria MarkupError. Bug B defense.
            safe_msg = self._safe_markup(display_msg)
            blocks.append(_TextBlock(text=f"[red]✗[/red] {safe_msg}", source="error"))

    @staticmethod
    def _find_tool_block(blocks: List[Any], tool_call_id: Optional[str]) -> Optional[_ToolBlock]:
        if not tool_call_id:
            return None
        for b in reversed(blocks):
            if isinstance(b, _ToolBlock) and b.tool_call_id == tool_call_id:
                return b
        return None

    # ------------------------------------------------------------------
    # Compose — turn block list into a Rich renderable for Live
    # ------------------------------------------------------------------

    def _compose(
        self,
        blocks: List[Any],
        footer: Optional[str] = None,
        spinner_frame: str = "⠋",
        force_complete: bool = False,
    ):
        # Visual separation rule: every rendered item gets a blank line
        # before it (except the first), so tool blocks, text blocks, and
        # the usage footer are never "glued together" on the screen.
        #
        # ``force_complete`` is set on the FINAL frame of the stream so any
        # trailing in-progress GFM table (no closing blank line received)
        # is rendered as a real Markdown table in the committed scrollback
        # instead of the dim raw-pipes placeholder we use during streaming.
        from rich.console import Group
        rendered: List[Any] = []

        def _push(item: Any) -> None:
            if rendered:
                rendered.append(Text(""))
            rendered.append(item)

        for b in blocks:
            if isinstance(b, _TextBlock):
                if not b.text:
                    continue
                if b.source == "validation_gate":
                    _push(Panel(
                        Text(b.text, style="yellow"),
                        title=_VALIDATION_GATE_TITLE,
                        border_style="yellow",
                    ))
                elif b.source == "error":
                    _push(Text.from_markup(b.text))
                elif self._markdown:
                    if force_complete:
                        prefix, tail = b.text, ""
                    else:
                        prefix, tail = safe_streaming_split(b.text)
                    if prefix:
                        try:
                            _push(Markdown(prefix))
                        except Exception:
                            _push(Text(prefix))
                    if tail:
                        # Show the in-progress table as raw dim pipes so
                        # the user sees the agent typing rows; we avoid
                        # re-parsing-as-table on every delta which causes
                        # the Live region to jump (header → 0-row table
                        # → 1-row table → …) as the parse outcome flips.
                        _push(Text(tail, style="dim"))
                else:
                    _push(Text(b.text))
            elif isinstance(b, _RenderableBlock):
                if b.renderable is not None:
                    _push(b.renderable)
            elif isinstance(b, _StageBlock):
                label = b.text or "Processando"
                if b.progress_total is not None and b.progress_total > 0:
                    current = b.progress_current if b.progress_current is not None else 0
                    if b.progress_label:
                        label = f"{b.progress_label} ({current}/{b.progress_total})"
                    else:
                        label = f"{label} ({current}/{b.progress_total})"
                    _push(Text.from_markup(f"[dim]{spinner_frame} {label}…[/dim]"))
                else:
                    _push(Text.from_markup(f"[dim]{spinner_frame} {label}…[/dim]"))
            elif isinstance(b, _ToolBlock):
                # Tools direct-print já tiveram cabeçalho impresso eagermente
                # na scrollback. Mostrá-los na Live region duplicaria visualmente
                # o cabeçalho durante a execução da bash.
                if b.head_committed:
                    continue
                _push(self._tool_renderable(b))
        if footer:
            _push(Text.from_markup(footer))
        return Group(*rendered) if rendered else Text("")

    def _tool_renderable(self, block: _ToolBlock):
        # Bug B defense: ``from_markup`` levanta ``MarkupError`` se o
        # display_name ou args injetados contiverem ``[`` literal (vindo
        # de nome de tool com colchete, ou args com markup acidental). O
        # fallback degrada para texto plano em vez de derrubar o frame.
        try:
            return Text.from_markup(self._tool_head_markup(block) + self._tool_summary_markup(block))
        except Exception:
            return Text(self._tool_head_plain(block) + self._tool_summary_plain(block))

    def _tool_head_markup(self, block: _ToolBlock) -> str:
        """Cabeçalho `● Name(args)` com cor conforme status."""
        display_name = self._safe_markup(
            _TOOL_DISPLAY_NAME.get(block.tool_name, block.tool_name)
        )
        args_inline = self._safe_markup(
            self._render_args_inline(block.tool_name, block.args)
        )
        if block.status == "running":
            return f"[yellow]●[/yellow] [bold]{display_name}[/bold]({args_inline}) [dim]running…[/dim]"
        if block.status == "success":
            return f"[green]●[/green] [bold]{display_name}[/bold]({args_inline})"
        return f"[red]●[/red] [bold]{display_name}[/bold]({args_inline})"

    def _tool_head_plain(self, block: _ToolBlock) -> str:
        """Versão plain-text do cabeçalho — fallback se ``from_markup`` falhar."""
        display_name = _TOOL_DISPLAY_NAME.get(block.tool_name, block.tool_name)
        args_inline = self._render_args_inline(block.tool_name, block.args)
        marker = {"running": "●", "success": "●", "error": "●"}.get(block.status, "●")
        suffix = " running…" if block.status == "running" else ""
        return f"{marker} {display_name}({args_inline}){suffix}"

    def _tool_summary_plain(self, block: _ToolBlock) -> str:
        """Versão plain-text do summary — fallback se ``from_markup`` falhar."""
        if not block.summary:
            return ""
        return f"\n  ⎿ {block.summary}"

    def _tool_summary_markup(self, block: _ToolBlock) -> str:
        """Linha `  ⎿ summary` quando há resumo; vazio caso contrário."""
        if not block.summary:
            return ""
        marker_color = "red" if block.status == "error" else "green"
        return (
            f"\n  [{marker_color}]⎿[/{marker_color}] "
            f"[dim]{self._safe_markup(block.summary)}[/dim]"
        )

    @staticmethod
    def _render_args_inline(tool_name: str, args: Optional[Dict[str, Any]]) -> str:
        """Args inline para o cabeçalho.

        Para tools com argumento primário (ex.: bash_execute → "command"),
        mostramos apenas o valor desse argumento, sem o nome da chave nem
        aspas — para ler `Bash(find /foo)` em vez de `Bash(command='find /foo')`.
        Para os demais, formato `chave=valor` com truncamento.
        """
        if not args:
            return ""

        formatter = _TOOL_ARG_FORMATTERS.get(tool_name)
        if formatter is not None:
            try:
                return formatter(args)
            except Exception:  # never let a custom formatter break rendering
                pass

        primary = _TOOL_PRIMARY_ARG.get(tool_name)
        if primary is not None and primary in args:
            value = str(args[primary])
            # Collapse newlines for snippets (python_execute code, multi-line
            # bash heredocs) so the header stays single-line.
            if "\n" in value:
                value = value.replace("\n", " ⏎ ")
            if len(value) > 80:
                value = value[:77] + "…"
            return value

        parts = []
        for k, v in list(args.items())[:3]:
            sv = str(v)
            if len(sv) > 40:
                sv = sv[:37] + "…"
            parts.append(f"{k}={sv!r}")
        if len(args) > 3:
            parts.append("…")
        return ", ".join(parts)

    @staticmethod
    def _safe_markup(text: str) -> str:
        # Defang accidental markup brackets so dim-rendered tool summaries
        # never inject color codes from tool data.
        return text.replace("[", "\\[")

    @staticmethod
    def _error_message(envelope: Any) -> str:
        if envelope is None:
            return "stream error"
        if isinstance(envelope, dict):
            return str(envelope.get("message") or envelope.get("error_type") or envelope)
        return str(getattr(envelope, "message", envelope))
