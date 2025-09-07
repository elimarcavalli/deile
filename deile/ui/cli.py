"""Interface CLI principal"""

from typing import Optional, Dict, Any
import os
import sys
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table

from ..core.agent import DeileAgent
from ..storage.logs import get_logger


class CLI:
    """Interface de linha de comando para o DEILE"""
    
    def __init__(self):
        self.console = Console()
        self.logger = get_logger()
        self.agent: Optional[DeileAgent] = None
    
    def initialize(self, agent: DeileAgent) -> None:
        """Inicializa CLI com instância do agente"""
        self.agent = agent
    
    def print_header(self) -> None:
        """Imprime cabeçalho da aplicação"""
        self.console.rule("[bold #4285F4]DEILE[/][bold #7B68EE] AI AGENT[/] [cyan]v5.0[/]", style="#4285F4")
        self.console.print("✨ [bold]Agente de IA modular e extensível![/bold]", justify="center")
        self.console.print("Digite 'help' para comandos disponíveis ou 'exit' para sair.", justify="center")
        self.console.rule(style="#4285F4")
    
    def print_response(self, response_content: str) -> None:
        """Exibe resposta do agente"""
        self.console.print("\n🤖 [bold #4285F4]DEILE:[/]")
        self.console.print("-" * 40)
        self.console.print(Markdown(response_content))
    
    def print_tool_results(self, tool_results: list) -> None:
        """Exibe resultados de ferramentas"""
        if not tool_results:
            return
        
        self.console.print(f"\n📊 Ferramentas executadas: {len(tool_results)}")
        
        for i, result in enumerate(tool_results, 1):
            status_emoji = "✅" if result.is_success else "❌"
            self.console.print(f"   {i}. {status_emoji} {result.message}")
    
    def print_stats(self, stats: Dict[str, Any]) -> None:
        """Exibe estatísticas do sistema"""
        self.console.print("\n📊 [bold]DEILE - Estatísticas[/bold]")
        self.console.print("=" * 50)
        
        # Informações gerais
        self.console.print(f"Status: {stats.get('status', 'unknown')}")
        self.console.print(f"Requisições: {stats.get('request_count', 0)}")
        self.console.print(f"Sessões ativas: {stats.get('active_sessions', 0)}")
        
        # Estatísticas de ferramentas
        tools_stats = stats.get('tools', {})
        if tools_stats:
            self.console.print(f"\n🔧 Ferramentas:")
            self.console.print(f"  Total: {tools_stats.get('total_tools', 0)}")
            self.console.print(f"  Habilitadas: {tools_stats.get('enabled_tools', 0)}")
        
        # Estatísticas de parsers
        parsers_stats = stats.get('parsers', {})
        if parsers_stats:
            self.console.print(f"\n📝 Parsers:")
            self.console.print(f"  Total: {parsers_stats.get('total_parsers', 0)}")
            self.console.print(f"  Habilitados: {parsers_stats.get('enabled_parsers', 0)}")
    
    def print_help(self) -> None:
        """Exibe ajuda"""
        self.console.print("\n📖 [bold]Ajuda - DEILE[/bold]")
        self.console.print("=" * 50)
        
        # Comandos básicos
        table = Table(title="Comandos Disponíveis")
        table.add_column("Comando", style="cyan", no_wrap=True)
        table.add_column("Descrição", style="white")
        
        table.add_row("help", "Mostra esta ajuda")
        table.add_row("stats", "Exibe estatísticas do sistema")
        table.add_row("clear", "Limpa a tela")
        table.add_row("exit/quit", "Sair do programa")
        
        self.console.print(table)
        
        # Operações com arquivos
        self.console.print("\n📄 [bold]Operações com Arquivos:[/bold]")
        self.console.print("  @arquivo.txt     - Referencia um arquivo")
        self.console.print("  'ler @file.py'   - Lê um arquivo")
        self.console.print("  'criar @novo.txt' - Cria um arquivo")
        self.console.print("  'listar arquivos' - Lista arquivos")
        
        # Exemplos
        self.console.print("\n💡 [bold]Exemplos:[/bold]")
        self.console.print("  'Mostre-me @README.md'")
        self.console.print("  'Analise @main.py'")
        self.console.print("  'Crie um script Python para web scraping'")
        self.console.print("  'Liste todos os arquivos Python'")
    
    def print_error(self, message: str) -> None:
        """Exibe mensagem de erro"""
        self.console.print(f"❌ [bold red]{message}[/bold red]")
    
    def print_warning(self, message: str) -> None:
        """Exibe mensagem de aviso"""
        self.console.print(f"⚠️  [bold yellow]{message}[/bold yellow]")
    
    def print_success(self, message: str) -> None:
        """Exibe mensagem de sucesso"""
        self.console.print(f"✅ [bold green]{message}[/bold green]")
    
    def print_info(self, message: str) -> None:
        """Exibe mensagem informativa"""
        self.console.print(f"ℹ️  [bold blue]{message}[/bold blue]")
    
    def get_user_input(self, prompt_text: str = "🤖 Você > ") -> str:
        """Obtém entrada do usuário"""
        try:
            return Prompt.ask(prompt_text).strip()
        except KeyboardInterrupt:
            return "exit"
        except EOFError:
            return "exit"
    
    def clear_screen(self) -> None:
        """Limpa a tela"""
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def print_goodbye(self) -> None:
        """Mensagem de despedida"""
        self.console.print("\n👋 [bold yellow]Obrigado por usar o DEILE![/bold yellow]")
        self.console.print("Até a próxima! 🚀")
    
    def confirm(self, message: str, default: bool = False) -> bool:
        """Solicita confirmação do usuário"""
        choices = "[y/N]" if not default else "[Y/n]"
        response = Prompt.ask(f"{message} {choices}").lower().strip()
        
        if not response:
            return default
        
        return response in ['y', 'yes', 'sim', 's']