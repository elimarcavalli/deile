import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List

# Adiciona o diretório do projeto ao Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Carrega variáveis de ambiente do arquivo .env
try:
    from dotenv import load_dotenv
    env_path = project_root / '.env'
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    # python-dotenv não instalado, ignora silenciosamente
    pass

try:
    from deile.config.settings import get_settings
    from deile.config.manager import ConfigManager
    from deile.storage.logs import get_logger
    from deile.core.agent import DeileAgent
    from deile.core.models.gemini_provider import GeminiProvider
    from deile.core.models.router import get_model_router
    from deile.tools.registry import get_tool_registry
    from deile.parsers.registry import get_parser_registry
    from deile.ui import ConsoleUIManager, UITheme, UIMessage, MessageType
except ImportError as e:
    print(f"❌ ERRO: Falha ao importar módulos do DEILE: {e}")
    print("\nCertifique-se de que você está executando o script a partir do diretório raiz do projeto.")
    sys.exit(1)


class DeileAgentCLI:
    """Classe principal para a Interface de Linha de Comando do Agente."""
    def __init__(self):
        self.settings = get_settings()
        self.logger = get_logger()
        self.config_manager = ConfigManager()
        self.ui = ConsoleUIManager(UITheme.DEFAULT, config_manager=self.config_manager)
        self.agent = None

    async def initialize(self) -> bool:
        """Inicializa o agente e todos os seus componentes."""
        try:
            self.ui.initialize()
            
            # Carrega configurações
            self.config_manager.load_config()
            
            with self.ui.show_loading("Inicializando DEILE v5.0..."):
                if not self.settings.get_api_key("gemini"):
                    self.ui.display_error(
                        "Chave de API do Google não encontrada!",
                        "Por favor, configure a variável de ambiente GOOGLE_API_KEY."
                    )
                    return False

                model_router = get_model_router()
                model_router.register_provider(GeminiProvider(), priority=1)
                
                tool_registry = get_tool_registry()
                parser_registry = get_parser_registry()

                self.agent = DeileAgent(
                    model_router=model_router,
                    tool_registry=tool_registry,
                    parser_registry=parser_registry,
                    config_manager=self.config_manager
                )

                # CORREÇÃO CRÍTICA: Inicializa PersonaManager
                await self.agent.initialize()

                # ROBUSTEZ: Cria sessão padrão persistente com working_directory correto
                self.default_session = self.agent.create_session(
                    session_id="default_cli_session",
                    working_directory=self.settings.working_directory
                )

            # Configura o autocompletar de arquivos com working_directory correto
            file_list = self._get_project_files()
            self.ui.setup_hybrid_completion(working_directory=str(self.settings.working_directory))
            self.ui.setup_file_completion(file_list)
            
            return True

        except Exception as e:
            self.ui.display_error(f"Falha fatal na inicialização do agente: {e}")
            self.logger.error(f"Inicialização do agente falhou: {e}", exc_info=True)
            return False

    async def run_interactive(self):
        """Executa o agente em modo interativo."""
        if not await self.initialize():
            return

        self.ui.show_welcome()
        
        try:
            while True:
                user_input = await asyncio.to_thread(self.ui.get_user_input, "\n > ")
                user_input = user_input.strip()
                ## pausar por 1 segundo para evitar processamento excessivo
                await asyncio.sleep(0.5)

                if not user_input:
                    continue
                if user_input.lower() in ['exit', 'quit', 'q']:
                    self.ui.display_message(UIMessage(content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]", message_type=MessageType.SYSTEM))
                    break

                with self.ui.show_loading("Processando sua solicitação..."):
                    response = await self.agent.process_input(
                        user_input=user_input,
                        session_id=self.default_session.session_id
                    )
                
                # Com Chat Sessions, tool executions estão integradas na resposta conversacional
                # A response.content já inclui informações sobre tools executadas automaticamente
                self.ui.display_response(response.content, {"execution_time": response.execution_time})
                
                # Opcionalmente, mostra tool executions como parte da conversa (modo debug ou verbose)
                if response.tool_results and getattr(self.settings, "show_tool_details", False):
                    self.ui.console.print("\n[dim]Tool executions:[/dim]")
                    for result in response.tool_results:
                        if result.metadata and "rich_display" in result.metadata:
                            self.ui.console.print(f"[dim]{result.metadata['rich_display']}[/dim]")
                        else:
                            icon = "[green]●[/green]" if result.is_success else "[red]●[/red]"
                            self.ui.console.print(f"[dim]{icon} {result.message}[/dim]")
        
        except (KeyboardInterrupt, EOFError):
            self.ui.display_message(UIMessage(content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]", message_type=MessageType.SYSTEM))
        except Exception as e:
            self.ui.display_error(f"Ocorreu um erro fatal no loop principal: {e}")
            self.logger.critical(f"Erro fatal no modo interativo: {e}", exc_info=True)

    def _get_project_files(self) -> List[str]:
        """Obtém a lista de arquivos do projeto para autocompletar."""
        project_files = []
        working_dir = Path(self.settings.working_directory)
        ignore_dirs = {'__pycache__', '.git', 'node_modules', '.venv', 'venv', 'requests', 'logs', 'dist', 'build', '.deile'}

        for file_path in working_dir.rglob('*'):
            if file_path.is_file() and not any(ignore_dir in file_path.parts for ignore_dir in ignore_dirs):
                relative_path = file_path.relative_to(working_dir)
                project_files.append(str(relative_path).replace('\\', '/'))
        
        return sorted(project_files)[:500] # Limita a 500 arquivos

def main():
    ##
    # os.system('cls' if os.name == 'nt' else 'clear')
    logging.disable()
    ##
    """Ponto de entrada principal da aplicação CLI."""
    cli = DeileAgentCLI()
    if len(sys.argv) > 1:
        # Modo de comando único (não implementado para esta fase)
        print("Execução de comando único será implementada em versões futuras.")
    else:
        # Modo interativo padrão
        asyncio.run(cli.run_interactive())

if __name__ == "__main__":
    main()