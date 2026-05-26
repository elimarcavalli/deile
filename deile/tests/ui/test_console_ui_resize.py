"""Regressão: o welcome screen e construções de UI devem adaptar à largura
do terminal no momento do render — não usar largura derivada de texto.

Issue #307: anteriormente, ``show_welcome`` calculava ``inner_w`` a partir do
maior `len(string)` e desenhava `╔══╗` manualmente, deixando a caixa travada
naquela largura. Mesmo terminal redimensionado, novos renders preservavam o
tamanho antigo. O fix: trocar o desenho manual por `Panel`/`Rule` do Rich,
que consultam `console.width` lazy em cada render.

Bug visual reportado pós-PR #312: ``Panel(...)`` default (``expand=True``)
fazia a caixa esticar pela largura inteira do terminal, parecendo "bordas
soltas" e cores quebradas em terminais largos. Fix complementar (após
#307): trocar para ``Panel.fit(...)`` (``expand=False``) — caixa COMPACTA
dimensionada ao conteúdo, mas ainda adaptativa: terminais menores que o
conteúdo (ex.: 30 cols) fazem o Rich clampar a caixa e o texto reflowar
naturalmente. A regra estrutural (proibido ``inner_w = max(len(...))``)
continua valendo: largura do conteúdo emerge do próprio conteúdo, não de
``len(string)`` hardcoded.

Limitação fundamental NÃO testada aqui (porque é inevitável): conteúdo já
commitado ao scrollback não reflowa. Esses testes cobrem apenas o
comportamento de NOVOS renders.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from deile.ui.console_ui import ConsoleUIManager


def _make_ui(width: int, default_model: str = "deepseek:deepseek-v4-pro") -> ConsoleUIManager:
    """Cria uma UI com Console de largura fixa explícita (simula terminal de N cols)."""
    cfg = SimpleNamespace(default_model=default_model)
    config_manager = SimpleNamespace(get_config=lambda: cfg)
    ui = ConsoleUIManager.__new__(ConsoleUIManager)
    ui.console = Console(
        file=io.StringIO(),
        width=width,
        force_terminal=True,
        color_system=None,
        record=True,
    )
    ui.session = None
    ui.is_initialized = True
    ui.config_manager = config_manager
    ui.working_directory = None
    return ui


def _panel_borders(output: str) -> tuple[str, str]:
    """Localiza topo e fundo do Panel de boas-vindas.

    Filtra com cuidado: a ASCII art ``_DEILE_ASCII`` contém substrings
    ``╔══╗``/``╚══╝`` (parte do logo). Em terminais estreitos Rich quebra
    essas linhas em fragmentos que ainda começam com ``╔``/``╚``. O Panel
    de boas-vindas é a ÚLTIMA caixa Unicode no output (vem após o logo);
    pegamos a última ocorrência de cada borda para isolá-lo.
    """
    lines = output.split("\n")
    top = next(
        ln for ln in reversed(lines) if ln.startswith("╔") and ln.endswith("╗")
    )
    bot = next(
        ln for ln in reversed(lines) if ln.startswith("╚") and ln.endswith("╝")
    )
    return top, bot


@pytest.mark.unit
@pytest.mark.parametrize("width", [80, 100, 120, 160, 200])
def test_show_welcome_box_is_compact_on_wide_terminals(width: int) -> None:
    """A caixa `╔══╗` do welcome NÃO ocupa a largura inteira em terminais largos.

    Bug visual pós-PR #312: ``Panel(...)`` (default ``expand=True``) fazia
    a caixa esticar pra largura total do terminal — em terminais largos
    o look ficava "solto" e quebrado. Fix: ``Panel.fit(...)`` produz uma
    caixa COMPACTA dimensionada ao conteúdo, igual ao desenho manual
    antigo (~48 chars), mas com largura ainda determinada lazy pelo
    Rich (sem ``len(string)`` literal).

    Topo e fundo devem ter o MESMO tamanho (caixa balanceada) e ser
    estritamente menores que a largura do console (look compacto). O
    tamanho exato emerge do conteúdo, mas em geral cabe em ~50 chars.
    """
    ui = _make_ui(width=width)
    ui.show_welcome()
    output = ui.console.file.getvalue()

    box_top, box_bot = _panel_borders(output)

    # Topo e fundo balanceados (caixa fechada nas 4 quinas).
    assert len(box_top) == len(box_bot), (
        f"box assimétrica: top={len(box_top)} bot={len(box_bot)}"
    )
    # E estritamente menor que a largura do console — compacto.
    assert len(box_top) < width, (
        f"caixa de largura {len(box_top)} ocupou TODO o console (w={width}); "
        f"deveria ser compacta (Panel.fit). Linha: {box_top!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("width", [30, 40])
def test_show_welcome_box_clamps_to_narrow_terminal(width: int) -> None:
    """Em terminais MAIS estreitos que o conteúdo, a caixa encolhe para
    caber sem estourar.

    ``Panel.fit`` ainda consulta ``console.width`` no render — quando o
    terminal é estreito demais para a largura natural do conteúdo, Rich
    clampa a caixa à largura do terminal e o texto reflow internamente.
    Garantia: nenhuma linha ultrapassa ``width``.
    """
    ui = _make_ui(width=width)
    ui.show_welcome()
    output = ui.console.file.getvalue()

    box_top, box_bot = _panel_borders(output)
    assert len(box_top) <= width, (
        f"caixa estourou: top={len(box_top)} > width={width}"
    )
    assert len(box_bot) <= width, (
        f"caixa estourou: bot={len(box_bot)} > width={width}"
    )


@pytest.mark.unit
def test_show_welcome_uses_double_box_style() -> None:
    """Mantemos box.DOUBLE (`╔══╗`) para preservar identidade visual."""
    ui = _make_ui(width=80)
    ui.show_welcome()
    output = ui.console.file.getvalue()
    assert "╔" in output and "╚" in output, (
        "expected DOUBLE box characters in welcome output"
    )
    # Separador interno: usamos `Rule` (que renderiza com `─` simples ligadas
    # nas bordas por `╟` / `╢`), não mais `╠══╣` manual.
    assert "╟" in output or "─" in output, (
        "expected horizontal separator (Rule) inside the panel"
    )


@pytest.mark.unit
def test_show_welcome_box_is_not_full_terminal_width_when_room_for_compact() -> None:
    """A caixa compacta (``Panel.fit``) NÃO ocupa a largura inteira do
    terminal quando ele é mais largo que o conteúdo.

    Substitui o teste antigo ``test_show_welcome_does_not_use_text_derived_width``
    que assumia ``Panel`` expansível (full-width). Agora o contrato é o
    inverso: a caixa fica compacta em terminais largos, e cresce/encolhe
    com o conteúdo — mas a regra estrutural (proibido literal
    ``inner_w = max(len(...))``) continua respeitada pelo uso de
    ``Panel.fit`` em vez de cálculo manual.

    Verificações:
    - Caixa < largura do console (não estica).
    - Conteúdo mais longo produz caixa proporcionalmente maior — não
      por mágica de ``len()`` mas porque o Rich mede o renderable e
      ``expand=False`` significa "use a medida natural".
    """
    short = _make_ui(width=200, default_model="x:short")
    short.show_welcome()
    long_ = _make_ui(
        width=200,
        default_model="anthropic:claude-opus-4-7-with-a-deliberately-very-long-suffix",
    )
    long_.show_welcome()

    short_top, _ = _panel_borders(short.console.file.getvalue())
    long_top, _ = _panel_borders(long_.console.file.getvalue())

    # Ambas COMPACTAS (estritamente menores que o console de 200 cols).
    assert len(short_top) < 200
    assert len(long_top) < 200
    # E a caixa do modelo longo é >= que a do curto (conteúdo dimensiona).
    assert len(long_top) >= len(short_top)


@pytest.mark.unit
def test_show_welcome_does_not_set_console_explicit_width() -> None:
    """A `Console` viva do `ConsoleUIManager` não deve travar `_width`.

    Se `_width` for setado no construtor, `Console.size` retorna esse valor
    em vez de chamar `os.get_terminal_size()` — quebra a adaptação a resize.
    """
    # ``ConsoleUIManager.__init__`` instancia o ``Console`` real. Verificamos
    # diretamente que o construtor não passa ``width=`` para Rich.
    from deile.ui.console_ui import ConsoleUIManager as _UI
    ui = _UI()
    # `Console._width` é o atributo privado setado pelo construtor quando o
    # caller passa `width=N` explicitamente. Deve ser `None` para Rich
    # detectar lazy a cada acesso.
    assert ui.console._width is None, (
        "Console foi instanciado com width explícito; isso impede adaptação a resize"
    )


@pytest.mark.unit
def test_show_welcome_panel_contains_provider_and_model() -> None:
    """Conteúdo semântico continua presente após o refactor."""
    ui = _make_ui(width=80, default_model="anthropic:claude-opus-4-7")
    ui.show_welcome()
    output = ui.console.file.getvalue()
    assert "Provider" in output
    assert "Anthropic" in output
    assert "Model" in output
    assert "claude-opus-4-7" in output
    assert "DEILE" in output
