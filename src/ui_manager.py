from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt as RichPrompt

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.lexers import PygmentsLexer
from pygments.lexers.shell import BashLexer

class PathCompleter(Completer):
    """Completer customizado para caminhos de arquivo que começam com @."""
    def __init__(self, file_paths):
        self.file_paths = [f"@{path}" for path in file_paths]

    def get_completions(self, document, complete_event):
        word_before_cursor = document.get_word_before_cursor(WORD=True)
        if word_before_cursor.startswith('@'):
            for path in self.file_paths:
                if path.startswith(word_before_cursor):
                    yield Completion(
                        path,
                        start_position=-len(word_before_cursor),
                        display=path.lstrip('@'),
                        display_meta="arquivo"
                    )

class UIManager:
    def __init__(self):
        self.console = Console()
        self.session = None

    def inicializar_session(self, file_list_cache):
        """Inicializa a sessão de prompt com a lista de arquivos para autocompletar."""
        self.session = PromptSession(
            lexer=PygmentsLexer(BashLexer),
            completer=PathCompleter(file_list_cache),
            complete_while_typing=True
        )

    def imprimir_cabecalho(self):
        """Imprime o cabeçalho da interface."""
        self.console.rule("[bold #4285F4]DEILE[/][bold #7B68EE] AI AGENT[/] [cyan]v3.1[/]", style="#4285F4")
        self.console.print("✨ [bold]Agente de IA pronto para analisar e otimizar o seu código![/bold]", justify="center")
        self.console.print("Digite [yellow]@[/yellow] para pesquisar e autocompletar arquivos do projeto.", justify="center")
        self.console.print("Use verbos como [green]'altere'[/green], [green]'crie'[/green] ou [green]'refatore'[/green] para modificar ou criar arquivos.", justify="center")
        self.console.print("Atalhos úteis no prompt: [bold]Ctrl+W[/bold] (apaga palavra) e [bold]Ctrl+_[/bold] (desfaz a digitação).", justify="center")
        self.console.print("Digite '[bold]sair[/bold]' ou '[bold]exit[/bold]' para encerrar a sessão.", justify="center")
        self.console.rule(style="#4285F4")

    def obter_prompt_usuario(self):
        """Obtém a entrada do usuário usando prompt_toolkit."""
        if not self.session:
            raise RuntimeError("A sessão de UI não foi inicializada. Chame `inicializar_session` primeiro.")
        return self.session.prompt([('class:prompt', 'Você > ')])

    def exibir_resposta_simples(self, texto_resposta):
        """Exibe a resposta da IA no console sem menção a arquivos de log."""
        self.console.print("\n--- [bold #4285F4]DEILE[/] ---")
        self.console.print(Markdown(texto_resposta))
        self.console.print("---"*10)

    def exibir_status(self, mensagem):
        """Exibe uma mensagem de status com um spinner."""
        return self.console.status(f"[bold #4285F4]{mensagem}[/bold #4285F4]", spinner="dots")

    def exibir_erro(self, mensagem):
        """Exibe uma mensagem de erro."""
        self.console.print(f"[bold red]ERRO: {mensagem}[/bold red]")
        
    def exibir_aviso(self, mensagem):
        """Exibe uma mensagem de aviso."""
        self.console.print(f"[yellow]Aviso: {mensagem}[/yellow]")
    
    def exibir_sucesso(self, mensagem):
        """Exibe uma mensagem de sucesso."""
        self.console.print(f"✅ [bold #4285F4]{mensagem}[/bold #4285F4]")

    def confirmar_sobrescrita(self, nome_arquivo):
        """Solicita confirmação do usuário para sobrescrever um arquivo."""
        confirmacao = RichPrompt.ask(f"[bold yellow]DEILE propôs alterações para o arquivo '{nome_arquivo}'. Deseja sobrescrevê-lo?[/bold yellow]", choices=["s", "n"], default="n")
        return confirmacao.lower() == 's'
    
    def atualizar_lista_arquivos(self, nova_lista_arquivos):
        """Atualiza o completer da sessão com uma nova lista de arquivos."""
        if self.session:
            self.session.completer = PathCompleter(nova_lista_arquivos)