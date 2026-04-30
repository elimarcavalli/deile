#!/usr/bin/env python3
"""Script Olá, Mundo! - Versão Colorida e Bonitinha 🌟"""

from rich import print as rprint
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.style import Style
from rich.layout import Layout
from rich.columns import Columns
from rich.table import Table
from rich.console import Console
from rich.theme import Theme
import random

console = Console()

# ── Tema personalizado ──
tema = Theme({
    "titulo": "bold bright_yellow",
    "destaque": "bold bright_cyan on blue",
    "mundo": "bold bright_green",
    "info": "italic bright_magenta",
    "borda": "dim white",
})

console = Console(theme=tema)

# ── CSS bonitinho via rich ──

def cabecalho():
    """Cabeçalho animado com arte ASCII."""
    arte = r"""
   ╔══════════════════════════════╗
   ║   🌟  Olá, Mundo!  🌟       ║
   ╚══════════════════════════════╝
    """
    texto = Text(arte, style="bold bright_yellow")
    rprint(Panel(texto, border_style="bright_cyan", title="🚀 DEILE", title_align="center"))

def saudacoes():
    """Várias saudações coloridas."""
    sauda = [
        ("🇧🇷  Olá, Mundo!", "bold green"),
        ("🇺🇸  Hello, World!", "bold blue"),
        ("🇫🇷  Bonjour, le Monde!", "bold magenta"),
        ("🇪🇸  ¡Hola, Mundo!", "bold yellow"),
        ("🇩🇪  Hallo, Welt!", "bold cyan"),
        ("🇮🇹  Ciao, Mondo!", "bold red"),
        ("🇯🇵  こんにちは、世界！", "bold bright_white"),
    ]
    random.shuffle(sauda)

    items = []
    for texto, estilo in sauda:
        t = Text()
        t.append(texto, style=estilo)
        items.append(Panel(t, border_style="dim white", padding=(0, 2)))

    rprint()
    rprint("[bold bright_yellow]🌍  Saudações ao redor do mundo:[/]")
    rprint(Columns(items, equal=True, expand=False))
    rprint()

def info_colorida():
    """Tabela com informações de cores e estilos."""
    tabela = Table(
        title="🎨 Paleta de Cores",
        border_style="bright_blue",
        title_style="bold bright_white",
        header_style="bold bright_yellow",
    )
    tabela.add_column("Estilo", style="bold", justify="center")
    tabela.add_column("Exemplo", justify="center")

    estilos = [
        ("bold red", "Vermelho"),
        ("bold green", "Verde"),
        ("bold blue", "Azul"),
        ("bold magenta", "Magenta"),
        ("bold cyan", "Ciano"),
        ("bold yellow", "Amarelo"),
        ("bold white", "Branco"),
        ("blink bold bright_red", "Piscando!"),
    ]
    for estilo, nome in estilos:
        tabela.add_row(nome, Text(f"■ {nome}", style=estilo))

    rprint(tabela)
    rprint()

def decoracao():
    """Decoração final."""
    rprint(Panel(
        "[bold bright_green]✨  Pronto pra codar!  [/bold bright_green][bright_cyan]🐍[/]",
        border_style="bright_magenta",
        subtitle="🚀 By DEILE",
        subtitle_align="right",
        padding=(1, 2),
    ))
    rprint()

def barra_progresso():
    """Simula uma barra de progresso só por diversão."""
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
    import time

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
        transient=True,
    ) as progress:
        tarefa = progress.add_task("[bright_cyan]Carregando o universo...", total=100)
        for _ in range(100):
            progress.update(tarefa, advance=1)
            time.sleep(0.02)

def main():
    """Função principal."""
    console.clear()
    cabecalho()
    barra_progresso()
    saudacoes()
    info_colorida()
    decoracao()
    console.print("[dim]Pressione Enter para sair...[/]", end="")
    input()

if __name__ == "__main__":
    main()
