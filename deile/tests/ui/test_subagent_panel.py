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
    st = SubAgentState(task=SubAgentTask(
        index=index, description=description,
        prompt="prompt placeholder com tamanho suficiente",
        **kw,
    ))
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
    blanks = sum(1 for r in grp.renderables
                 if isinstance(r, _T) and not str(r).strip())
    # Header + 2 separadores entre 3 painéis + 1 antes da hint = ≥3 blanks
    assert blanks >= 3


def test_focus_layout_renders_ficha_with_description_and_prompt():
    states = [
        _mk_state(1, "refator complexo do módulo X", status="running",
                  persona="architect", model="claude-opus"),
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
