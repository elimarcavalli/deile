"""Regressão: o welcome screen e construções de UI devem adaptar à largura
do terminal no momento do render — não usar largura derivada de texto.

Issue #307: anteriormente, ``show_welcome`` calculava ``inner_w`` a partir do
maior `len(string)` e desenhava `╔══╗` manualmente, deixando a caixa travada
naquela largura. Mesmo terminal redimensionado, novos renders preservavam o
tamanho antigo. O fix: trocar o desenho manual por `Panel`/`Rule` do Rich,
que consultam `console.width` lazy em cada render.

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
@pytest.mark.parametrize("width", [20, 30, 40, 60, 80, 100, 120, 160, 200])
def test_show_welcome_box_adapts_to_console_width(width: int) -> None:
    """A caixa `╔══╗` do welcome usa a largura do console em cada render.

    Antes do fix da issue #307, a caixa ficava travada em ~48 chars (o
    `len()` do maior label) independentemente da largura do terminal.
    Larguras extremas (20, 30) também são incluídas para garantir que
    o texto interno quebra naturalmente sem crash — a caixa em si
    ainda assume a largura disponível, e o conteúdo refluí dentro.
    """
    ui = _make_ui(width=width)
    ui.show_welcome()
    output = ui.console.file.getvalue()

    box_top, box_bot = _panel_borders(output)

    # Topo e fundo devem ter exatamente a largura do console
    # (Panel default usa a largura total disponível).
    assert len(box_top) == width, (
        f"box top has len={len(box_top)} for console width={width}: {box_top!r}"
    )
    assert len(box_bot) == width, (
        f"box bottom has len={len(box_bot)} for console width={width}: {box_bot!r}"
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
def test_show_welcome_does_not_use_text_derived_width() -> None:
    """A largura da caixa NÃO deve depender do comprimento das strings exibidas.

    Renderizamos com um modelo muito longo e um curto na mesma largura de
    console e validamos que o topo do box tem o mesmo tamanho (= console.width).
    """
    short = _make_ui(width=120, default_model="x:short")
    short.show_welcome()
    long_ = _make_ui(
        width=120,
        default_model="anthropic:claude-opus-4-7-with-a-deliberately-very-long-suffix",
    )
    long_.show_welcome()

    short_top, _ = _panel_borders(short.console.file.getvalue())
    long_top, _ = _panel_borders(long_.console.file.getvalue())

    # Mesma largura de console → mesma largura de box, independente do texto.
    assert len(short_top) == len(long_top) == 120


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
