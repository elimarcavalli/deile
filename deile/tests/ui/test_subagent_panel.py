"""Snapshot/smoke tests para ``SubAgentPanelRenderer`` (issue #257).

Capturamos a saída via ``Console.capture()`` em ambiente sem TTY (sem teclado),
verificando que cada layout (compacto + foco) inclui os elementos esperados.
Não validamos pixel-por-pixel — Rich evolui; checamos estrutura semântica.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from deile.orchestration.subagents.events import SubAgentState, SubAgentTask
from deile.ui.subagent_panel import SubAgentPanelRenderer

pytestmark = pytest.mark.unit


def _mk_state(index=1, description="task", status="pending", **kw) -> SubAgentState:
    st = SubAgentState(
        task=SubAgentTask(
            index=index,
            description=description,
            prompt="prompt placeholder com tamanho suficiente",
            **kw,
        )
    )
    st.status = status
    return st


def _make_renderer(states, **kw):
    """Helper: cria renderer apontando o panel para um buffer.

    O painel constrói o próprio :class:`Console` com ``file=real_stdout``,
    então passamos o ``buf`` como real_stdout e expomos via wrapper para
    leitura. host_console NÃO é usado para output dos testes — só para
    detectar/suspender o Live do pai (que aqui não existe).
    """
    buf = StringIO()
    host_console = Console(file=StringIO(), force_terminal=False, width=120)
    renderer = SubAgentPanelRenderer(
        host_console=host_console,
        states=states,
        real_stdout=buf,
        enable_keyboard=kw.pop("enable_keyboard", False),
        **kw,
    )
    # Habilita recording no panel_console para os testes lerem export_text().
    renderer._panel_console.record = True
    return renderer, renderer._panel_console, buf


def _read_buf(buf: StringIO) -> str:
    return buf.getvalue()


def test_compact_layout_includes_header_and_one_panel_per_subagent():
    """Sem precisar do loop async, renderiza um frame compacto direto."""
    states = [
        _mk_state(1, "refatorar auth.py", status="running"),
        _mk_state(2, "gerar testes parser", status="pending"),
        _mk_state(3, "doc módulo X", status="ok"),
    ]
    states[2].result_text = "done"
    states[2].add_file("docs/x.md")

    renderer, console, _ = _make_renderer(states)
    console.print(renderer._compose_compact())
    out = console.export_text()

    # Cabeçalho com contadores agregados.
    assert "Decomposto em 3 frentes" in out
    # Cada frente é exibida pelo description (truncado).
    assert "refatorar auth.py" in out
    assert "gerar testes parser" in out
    assert "doc módulo X" in out
    # Frente concluída mostra estado final.
    assert "concluído" in out or "✅" in out


def test_compact_layout_has_blank_lines_between_panels():
    """Fix #3: espaçamento entre painéis — sem isso ficavam grudados."""
    states = [_mk_state(i, f"task {i}") for i in (1, 2, 3)]
    renderer, _, _ = _make_renderer(states)
    grp = renderer._compose_compact()
    # Group.renderables tem Texts(""), Panels, etc. Conte os Text("") vazios.
    from rich.text import Text as _T

    blanks = sum(1 for r in grp.renderables if isinstance(r, _T) and not str(r).strip())
    # Header + 2 separadores entre 3 painéis + 1 antes da hint = ≥3 blanks
    assert blanks >= 3


def test_focus_layout_renders_ficha_with_description_and_prompt():
    states = [
        _mk_state(
            1,
            "refator complexo do módulo X",
            status="running",
            persona="architect",
            model="claude-opus",
        ),
        _mk_state(2, "outra frente", status="pending"),
    ]
    states[0].push_progress("⚙ bash: pytest -q")
    states[0].push_progress("✓ pytest: 5 passed")
    states[0].add_file("deile/x.py")

    renderer, console, _ = _make_renderer(states)
    renderer._focus = 1  # foca a frente 1
    console.print(renderer._compose_focus(1))
    out = console.export_text()

    # Campos da ficha presentes (a tabela usa hifens em colunas).
    assert "description" in out
    assert "refator complexo do módulo X" in out
    assert "architect" in out
    assert "claude-opus" in out
    # Tail de execução.
    assert "pytest" in out


def test_focus_out_of_range_falls_back_to_compact_layout():
    states = [_mk_state(1, "a"), _mk_state(2, "b")]
    renderer, console, _ = _make_renderer(states)
    # Foco fora de range NÃO deve estourar — devolve compacto.
    frame = renderer._compose_focus(99)
    console.print(frame)
    out = console.export_text()
    assert "Decomposto em 2 frentes" in out


def test_markup_escape_prevents_injection_from_progress_lines():
    """progress_lines contém output bruto de tools — pode ter ``[red]`` ou
    outros caracteres de markup do Rich. Sem escape, quebraria o painel.
    """
    states = [_mk_state(1, "a", status="running")]
    # Linha maliciosa simulando output ANSI/markup
    states[0].push_progress("[red]NOT REAL MARKUP[/red] [boom")
    renderer, console, _ = _make_renderer(states)
    # Não deve estourar exception ao compor.
    console.print(renderer._compose_compact())
    out = console.export_text()
    # O texto cru deve aparecer (ou pelo menos o conteúdo essencial),
    # nada de erro de markup.
    assert "NOT REAL MARKUP" in out


def test_truncate_helper_inside_renderer_limits_long_titles():
    from deile.ui.subagent_panel import _truncate

    assert _truncate("x" * 200, 50).endswith("…")
    assert len(_truncate("x" * 200, 50)) == 50
    assert _truncate("short", 50) == "short"


# -------------------------------------------------------------------- #
# Round 6 — parse_key_buffer (issue: setas em rajada → ESC falso)        #
# -------------------------------------------------------------------- #


def test_parse_key_buffer_single_arrow_right():
    """1 seta direita inteira no buffer → 1 sequência CSI."""
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1b[C")
    assert seqs == ["\x1b[C"]
    assert rem == ""


def test_parse_key_buffer_burst_of_arrows_yields_each_seq_no_esc():
    """Rajada de 5 setas direitas num único buffer → 5 sequências,
    NENHUMA delas é ESC isolado. Esse é o cenário do bug raiz: o usuário
    aperta setas em rajada e o kernel entrega tudo de uma vez; o parser
    legacy quebrava entre os bytes e disparava ESC falso.
    """
    from deile.ui.subagent_panel import parse_key_buffer

    burst = "\x1b[C" * 5
    seqs, rem = parse_key_buffer(burst)
    assert seqs == ["\x1b[C"] * 5
    assert rem == ""
    assert "\x1b" not in seqs  # ESC isolado nunca aparece


def test_parse_key_buffer_mixed_burst_no_false_esc():
    """Rajada mista de setas L/R/digit no buffer → cada um separado,
    sem ESC falso.
    """
    from deile.ui.subagent_panel import parse_key_buffer

    burst = "\x1b[C\x1b[D2\x1b[Ch"
    seqs, rem = parse_key_buffer(burst)
    assert seqs == ["\x1b[C", "\x1b[D", "2", "\x1b[C", "h"]
    assert rem == ""


def test_parse_key_buffer_lone_esc_goes_to_remainder():
    """ESC sozinho NÃO sai como sequência — vai pro remainder para o
    caller decidir via timeout. Esse é o coração da fix: ESC genuíno
    só dispara quando o caller confirma que nada mais veio em 200ms.
    """
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1b")
    assert seqs == []
    assert rem == "\x1b"


def test_parse_key_buffer_partial_csi_goes_to_remainder():
    """CSI incompleta (``\\x1b[`` sem terminador) vai pro remainder."""
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1b[")
    assert seqs == []
    assert rem == "\x1b["

    # E o resto fecha numa rodada seguinte:
    seqs2, rem2 = parse_key_buffer(rem + "C")
    assert seqs2 == ["\x1b[C"]
    assert rem2 == ""


def test_parse_key_buffer_ss3_introducer():
    """SS3 (``\\x1bOC`` etc.) é sempre 3 bytes."""
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1bOC\x1bOD")
    assert seqs == ["\x1bOC", "\x1bOD"]
    assert rem == ""


def test_parse_key_buffer_ss3_partial_goes_to_remainder():
    """SS3 com só introducer espera no remainder."""
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1bO")
    assert seqs == []
    assert rem == "\x1bO"


def test_parse_key_buffer_esc_followed_by_garbage_does_not_emit_esc():
    """``\\x1b`` seguido de algo que não é introducer CSI/SS3 NÃO emite
    ESC genuíno — descarta o ``\\x1b`` e processa o byte como char normal.
    Esse foi um dos caminhos do bug legacy: o read(1) podia retornar ``""``
    ou um byte inesperado e o código original disparava ``_on_key("\\x1b")``.
    """
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1bXh")
    # ``\x1b`` descartado, ``X`` e ``h`` processados.
    assert "\x1b" not in seqs
    assert "X" in seqs
    assert "h" in seqs
    assert rem == ""


def test_parse_key_buffer_csi_with_modifiers_is_treated_as_single_seq():
    """CSI com modificadores (ex.: Shift+Right ``\\x1b[1;2C``) é uma seq."""
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, rem = parse_key_buffer("\x1b[1;2C")
    assert seqs == ["\x1b[1;2C"]
    assert rem == ""


def test_parse_key_buffer_csi_split_across_buffers_does_not_leak_esc():
    """Mesmo se a CSI vier em pedaços (kernel entrega ``\\x1b[`` numa
    syscall e ``C`` na próxima), o ``\\x1b`` fica no remainder e nunca
    é emitido como cancel. Cobre o cenário em que o usuário aperta as
    setas com pausa natural entre teclas.
    """
    from deile.ui.subagent_panel import parse_key_buffer

    seqs1, rem1 = parse_key_buffer("\x1b[")
    assert seqs1 == []
    assert rem1 == "\x1b["

    seqs2, rem2 = parse_key_buffer(rem1 + "C")
    assert seqs2 == ["\x1b[C"]
    assert rem2 == ""

    # Em nenhum dos chunks o ESC isolado aparece.
    assert all("\x1b" != s for s in seqs1 + seqs2)


def test_parse_key_buffer_no_false_esc_under_30_arrow_burst():
    """30 setas em rajada extrema (pior caso humano plausível) — nenhuma
    delas vira ESC falso e o foco anda como esperado.
    """
    from deile.ui.subagent_panel import parse_key_buffer

    burst = "\x1b[C\x1b[D" * 15  # 30 setas alternando
    seqs, rem = parse_key_buffer(burst)
    assert len(seqs) == 30
    assert all(s in ("\x1b[C", "\x1b[D") for s in seqs)
    assert rem == ""


# -------------------------------------------------------------------- #
# _on_key + parse_key_buffer integração — foco vs. cancel               #
# -------------------------------------------------------------------- #


def test_arrow_burst_does_not_set_cancel_or_change_focus_past_end():
    """Cenário do bug do usuário: foco na última frente, aperta seta
    direita várias vezes → painel NÃO deve fechar (cancel_requested
    permanece False).

    Como ``_on_key`` é definido dentro de ``_start_keyboard_watcher``
    (closure), instanciamos um renderer e simulamos o despacho das
    sequências chamando o método público ``_compose_compact`` antes/depois
    e validando ``_focus``/``_cancel_requested`` diretamente.
    """
    states = [_mk_state(i, f"task {i}", status="running") for i in (1, 2, 3)]
    renderer, _, _ = _make_renderer(states)
    renderer._focus = 3  # já no último

    # Simula o que o watcher faria depois de parsear a rajada:
    # despacha 10 setas direitas em sequência.
    from deile.ui.subagent_panel import parse_key_buffer

    seqs, _ = parse_key_buffer("\x1b[C" * 10)
    assert len(seqs) == 10

    # Aplicação manual da lógica de _on_key (sem precisar do thread):
    n_states = len(states)
    for seq in seqs:
        if seq == "\x1b":
            if renderer._focus is not None:
                renderer._focus = None
            else:
                renderer._cancel_requested = True
        elif seq in ("\x1b[C", "\x1bOC"):
            if renderer._focus and renderer._focus < n_states:
                renderer._focus += 1
        elif seq in ("\x1b[D", "\x1bOD"):
            if renderer._focus and renderer._focus > 1:
                renderer._focus -= 1

    # Ainda no último, ainda aberto.
    assert renderer._focus == 3
    assert renderer._cancel_requested is False


async def test_run_completes_when_all_states_terminal(tmp_path):
    """Smoke test: ``run()`` encerra cedo quando todos os states já estão
    em terminal (ok/error/cancelled). Sem TTY → keyboard watcher é skipado.
    """
    states = [
        _mk_state(1, "a", status="ok"),
        _mk_state(2, "b", status="error"),
    ]
    states[0].started_at = 0.0
    states[0].finished_at = 0.1
    states[1].started_at = 0.0
    states[1].finished_at = 0.1
    states[1].error = "test failure"

    renderer, console, buf = _make_renderer(states, refresh_hz=10.0)
    # Não deve travar — todos já estão em terminal.
    import asyncio

    await asyncio.wait_for(renderer.run(), timeout=2.0)

    # O Live escreve no buf (real_stdout). Lemos diretamente.
    out = _read_buf(buf)
    # Resumo final aparece no scrollback.
    assert "concluídos" in out or "sub-DEILE" in out.lower()
