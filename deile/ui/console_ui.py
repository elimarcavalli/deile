import time
from typing import List, Optional, Dict, Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.status import Status
from rich.prompt import Prompt as RichPrompt
from rich.table import Table
from rich import box

from prompt_toolkit import PromptSession

from .base import UIManager, UITheme, UIMessage, MessageType, UIStatus
from .completers import HybridCompleter


class ConsoleUIManager(UIManager):
    """Implementação da UI de console usando Rich e prompt_toolkit."""

    def __init__(self, theme: UITheme = UITheme.DEFAULT, config_manager=None):
        super().__init__(theme)
        # Console com configuração robusta para Windows
        import sys
        import io
        
        # Força codificação UTF-8 para stdout se possível
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except:
                pass
                
        self.console = Console(
            force_terminal=True,
            legacy_windows=True,
            _environ={"TERM": "ansi"},
            file=sys.stdout
        )
        self.session: Optional[PromptSession] = None
        self.is_initialized = False
        self.config_manager = config_manager
        self.working_directory = None

    def initialize(self) -> None:
        """Inicializa a UI."""
        self.is_initialized = True

    def setup_file_completion(self, file_paths: List[str]) -> None:
        """Configura autocompletar (compatibilidade) - usa HybridCompleter."""
        self.setup_hybrid_completion(working_directory=self.working_directory)
    
    def setup_hybrid_completion(self, working_directory: Optional[str] = None) -> None:
        """Configura o HybridCompleter para @ (arquivos) e / (comandos)."""
        if working_directory:
            self.working_directory = working_directory
        
        # SOLUÇÃO ROBUSTA: Fallback completo para Windows
        try:
            # Primeiro tenta verificar se estamos em um terminal compatível
            # import os
            # if os.name == 'nt' and 'TERM' not in os.environ:
            #     # Windows sem terminal ANSI adequado - usa fallback direto
            #     self.session = None
            #     return
                
            hybrid_completer = HybridCompleter(
                config_manager=self.config_manager,
                working_directory=self.working_directory
            )
            
            # Tenta configuração mais compatível com Windows
            from prompt_toolkit import PromptSession
            from prompt_toolkit.output import ColorDepth
            
            self.session = PromptSession(
                completer=hybrid_completer,
                complete_while_typing=True,  # Desabilita para evitar problemas
                color_depth=ColorDepth.DEPTH_1_BIT  # Cores básicas apenas
            )
            
        except Exception as e:
            # Fallback completo - sem prompt_toolkit 
            self.session = None

    def show_welcome(self):
        """Mostra a tela de boas-vindas formatada."""
        self.console.clear()
        title = Text.from_markup("[bold #4285F4]DEILE[/][bold #7B68EE] AI AGENT[/] [cyan]v5.0[/]")
        
        info_lines = [
            ":sparkles: [bold]Estou pronto para analisar e otimizar o seu código![/bold]",
            "\n[yellow]@[/yellow] para pesquisar arquivos do projeto.",
            "[cyan]/[/cyan] para acessar comandos especiais ([cyan]/help[/cyan], [cyan]/clear[/cyan], [cyan]/status[/cyan], etc.)",
            "Verbos como [green]'altere'[/green], [green]'crie'[/green] ou [green]'refatore'[/green] para modificar ou criar arquivos.",
            "\nAtalhos úteis: [bold]Ctrl+W[/bold] (apaga palavra) e [bold]Ctrl+_[/bold] (desfaz a digitação).",
            "Digite '[bold]sair[/bold]' ou '[bold]exit[/bold]' para encerrar a sessão."
        ]
        
        # CORREÇÃO ROBUSTA: Implementação com fallback completo
        def safe_print_panel(lines_to_try):
            try:
                info_panel = Text.from_markup("\n".join(lines_to_try), justify="left")
                panel = Panel(
                    info_panel,
                    title=title,
                    title_align="center",
                    border_style="#4285F4",
                    padding=(1, 2)
                )
                self.console.print(panel)
                return True
            except (UnicodeEncodeError, Exception):
                return False
        
        # Tenta com emojis primeiro
        if not safe_print_panel(info_lines):
            # Fallback sem emojis
            safe_info_lines = [line.replace(":sparkles:", "* ").strip() for line in info_lines]
            if not safe_print_panel(safe_info_lines):
                # Fallback final - texto simples
                try:
                    print("\n" + "="*30)
                    print("DEILE AI AGENT v5.0")
                    print("="*30)
                    for line in safe_info_lines:
                        # Remove markup Rich completamente
                        clean_line = line
                        clean_line = clean_line.replace("[bold]", "").replace("[/bold]", "")
                        clean_line = clean_line.replace("[yellow]", "").replace("[/yellow]", "")
                        clean_line = clean_line.replace("[cyan]", "").replace("[/cyan]", "")
                        clean_line = clean_line.replace("[green]", "").replace("[/green]", "")
                        clean_line = clean_line.replace("[/]", "")
                        print(clean_line.strip())
                    print("="*30 + "\n")
                except:
                    print("DEILE AI AGENT v5.0 - Ready")

    def get_user_input(self, prompt: str = "\n [bold green]>[/bold] ") -> str:
        """Obtém a entrada do usuário de forma interativa."""
        if not self.session:
            # Fallback para input simples removendo markup
            clean_prompt = prompt.replace("[bold green]", "").replace("[/bold]", "").replace("[/]", "")
            
            if not clean_prompt.startswith("\n"):
                clean_prompt = "\n" + clean_prompt

            return input(clean_prompt)
        
        try:
            return self.session.prompt([('class:prompt', "\n> ")])
        except Exception:
            # Se prompt_toolkit falhar, usa input simples
            return input("\n> ")

    def display_response(self, content, metadata: Optional[Dict] = None):
        """Exibe a resposta do agente com metadados."""
        self.console.print("\n[bold #4285F4]Deile >[/] ")
        
        # Verifica se é um objeto Rich (Panel, Table, etc.)
        if hasattr(content, '__rich__') or hasattr(content, '__rich_console__'):
            # É um objeto Rich - renderiza diretamente
            self.console.print(content)
        elif isinstance(content, str):
            # É string - usa Markdown
            self.console.print(Markdown(content))
        else:
            # Fallback - converte para string
            self.console.print(str(content))
        
        if metadata and (exec_time := metadata.get("execution_time")) is not None:
            self.console.print(f"\n:hourglass: [dim]{exec_time:.2f}s[/dim]")
        self.console.print("---"*20)

    def display_message(self, message: UIMessage):
        """Exibe uma mensagem simples com base no seu tipo."""
        # CORREÇÃO 2: A função console.print() já interpreta markup por padrão.
        # Remover o argumento 'style' permite que os emojis e cores no próprio texto sejam renderizados.
        self.console.print(message.content)

    def display_error(self, error: str, details: Optional[str] = None):
        """Exibe um erro formatado com fallback robusto."""
        try:
            # Primeira tentativa com emoji
            error_text = Text.from_markup(f":x: [bold red]ERRO:[/bold red] {error}")
            panel = Panel(error_text, border_style="red", title="[bold red]Ocorreu um Problema[/bold red]")
            if details:
                panel.renderable = Text.from_markup(f":x: [bold red]ERRO:[/bold red] {error}\n\n[dim]{details}[/dim]")
            self.console.print(panel)
        except (UnicodeEncodeError, Exception) as e:
            try:
                # Fallback sem emoji
                error_text = Text.from_markup(f"[!] [bold red]ERRO:[/bold red] {error}")
                panel = Panel(error_text, border_style="red", title="[bold red]Ocorreu um Problema[/bold red]")
                if details:
                    panel.renderable = Text.from_markup(f"[!] [bold red]ERRO:[/bold red] {error}\n\n[dim]{details}[/dim]")
                self.console.print(panel)
            except Exception:
                # Fallback final - texto simples
                try:
                    print("=" * 50)
                    print("ERRO:", error)
                    if details:
                        print("Detalhes:", details)
                    print("=" * 50)
                except:
                    print("ERRO ocorreu na exibição")

    def show_loading(self, message: str) -> Status:
        """Mostra uma animação de 'carregando' de forma segura."""
        return self.console.status(f"[bold cyan]{message}[/]", spinner="line")

    def display_success(self, message: str) -> None:
        """Exibe uma mensagem de sucesso formatada."""
        self.console.print(f":white_check_mark: [green]{message}[/green]")

    def display_status(self, status: UIStatus) -> None:
        """Exibe uma mensagem de status simples."""
        self.console.print(f":information: [cyan]STATUS:[/cyan] {status.message}")

    def confirm_action(self, message: str, default: bool = False) -> bool:
        """Solicita confirmação do usuário para uma ação."""
        return RichPrompt.ask(f"[bold yellow]{message}[/bold yellow]", choices=["s", "n"], default="n" if not default else "s").lower() == 's'

    def display_stats(self, stats: Dict[str, Any]) -> None:
        """Exibe as estatísticas do sistema em uma tabela."""
        table = Table(box=box.ROUNDED, title="[bold]Estatísticas do DEILE[/bold]")
        table.add_column("Métrica", style="cyan")
        table.add_column("Valor", style="magenta")

        for key, value in stats.items():
            if isinstance(value, dict):
                table.add_row(f"[bold]{key.replace('_', ' ').title()}[/bold]", "")
                for sub_key, sub_value in value.items():
                    table.add_row(f"  {sub_key.replace('_', ' ').title()}", str(sub_value))
            else:
                table.add_row(key.replace('_', ' ').title(), str(value))
        
        self.console.print(table)

    def cleanup(self) -> None:
        """Realiza a limpeza de recursos da UI ao sair."""
        pass