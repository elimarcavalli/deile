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


def test_compact_layout_includes_header_and_one_panel_per_subagent():
    """Sem precisar do loop async, renderiza um frame compacto direto."""
    console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
    states = [
        _mk_state(1, "refatorar auth.py", status="running"),
        _mk_state(2, "gerar testes parser", status="pending"),
        _mk_state(3, "doc módulo X", status="ok"),
    ]
    states[2].result_text = "done"
    states[2].add_file("docs/x.md")

    renderer = SubAgentPanelRenderer(console, states, enable_keyboard=False)
    frame = renderer._compose_compact()
    console.print(frame)
    out = console.export_text()

    # Cabeçalho com contadores agregados.
    assert "Decomposto em 3 frentes" in out
    # Cada frente é exibida pelo description (truncado).
    assert "refatorar auth.py" in out
    assert "gerar testes parser" in out
    assert "doc módulo X" in out
    # Frente concluída mostra estado final.
    assert "concluído" in out or "✅" in out


def test_focus_layout_renders_ficha_with_description_and_prompt():
    console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
    states = [
        _mk_state(1, "refator complexo do módulo X", status="running",
                  persona="architect", model="claude-opus"),
        _mk_state(2, "outra frente", status="pending"),
    ]
    states[0].push_progress("⚙ bash: pytest -q")
    states[0].push_progress("✓ pytest: 5 passed")
    states[0].add_file("deile/x.py")

    renderer = SubAgentPanelRenderer(console, states, enable_keyboard=False)
    renderer._focus = 1  # foca a frente 1
    frame = renderer._compose_focus(1)
    console.print(frame)
    out = console.export_text()

    # Campos da ficha presentes (a tabela usa hifens em colunas).
    assert "description" in out
    assert "refator complexo do módulo X" in out
    assert "architect" in out
    assert "claude-opus" in out
    # Tail de execução.
    assert "pytest" in out


def test_focus_out_of_range_falls_back_to_compact_layout():
    console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
    states = [_mk_state(1, "a"), _mk_state(2, "b")]
    renderer = SubAgentPanelRenderer(console, states, enable_keyboard=False)
    # Foco fora de range NÃO deve estourar — devolve compacto.
    frame = renderer._compose_focus(99)
    # Tipo Group — apenas confirma que algo renderizável foi devolvido.
    console.print(frame)
    out = console.export_text()
    assert "Decomposto em 2 frentes" in out


def test_truncate_helper_inside_renderer_limits_long_titles():
    from deile.ui.subagent_panel import _truncate
    assert _truncate("x" * 200, 50).endswith("…")
    assert len(_truncate("x" * 200, 50)) == 50
    assert _truncate("short", 50) == "short"


async def test_run_completes_when_all_states_terminal(tmp_path):
    """Smoke test: ``run()`` encerra cedo quando todos os states já estão
    em terminal (ok/error/cancelled). Sem TTY → keyboard watcher é skipado.
    """
    console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
    states = [
        _mk_state(1, "a", status="ok"),
        _mk_state(2, "b", status="error"),
    ]
    states[0].started_at = 0.0
    states[0].finished_at = 0.1
    states[1].started_at = 0.0
    states[1].finished_at = 0.1
    states[1].error = "test failure"

    renderer = SubAgentPanelRenderer(
        console, states, enable_keyboard=False, refresh_hz=10.0,
    )
    # Não deve travar — todos já estão em terminal.
    import asyncio
    await asyncio.wait_for(renderer.run(), timeout=2.0)

    out = console.export_text()
    # Resumo final aparece no scrollback.
    assert "concluídos" in out or "sub-DEILE" in out.lower()
