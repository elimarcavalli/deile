import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

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
    from deile.core.models.router import get_model_router
    from deile.core.models.bootstrap import bootstrap_providers
    from deile.tools.registry import get_tool_registry
    from deile.parsers.registry import get_parser_registry
    from deile.ui import ConsoleUIManager, UITheme, UIMessage, MessageType
except ImportError as e:
    print(f"❌ ERRO: Falha ao importar módulos do DEILE: {e}")
    print("\nCertifique-se de que você está executando o script a partir do diretório raiz do projeto.")
    sys.exit(1)

_MODEL_PROVIDERS_YAML = Path(__file__).parent / "deile" / "config" / "model_providers.yaml"


def _use_legacy_gemini_only() -> bool:
    """Return True when model_providers.yaml sets feature_flags.use_legacy_gemini_only=true."""
    try:
        import yaml
        with open(_MODEL_PROVIDERS_YAML) as f:
            data = yaml.safe_load(f)
        return bool(data.get("feature_flags", {}).get("use_legacy_gemini_only", False))
    except Exception:
        return False


def _bootstrap_legacy_gemini(router) -> list:
    """Register only GeminiProvider via the legacy path.

    Returns a list with the single provider_id registered, or an empty list on failure.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return []
    try:
        from deile.core.models.gemini_provider import GeminiProvider  # type: ignore
        provider = GeminiProvider()
        router.register_provider(provider, priority=1)
        return ["gemini"]
    except Exception as exc:
        logging.getLogger(__name__).error("Legacy Gemini bootstrap failed: %s", exc)
        return []


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
            
            with self.ui.show_loading("Inicializando DEILE v5.1..."):
                model_router = get_model_router()

                if _use_legacy_gemini_only():
                    registered = _bootstrap_legacy_gemini(model_router)
                else:
                    registered = bootstrap_providers(router=model_router)
                if not registered:
                    self.ui.display_error(
                        "Nenhum provider configurado.",
                        "Defina ao menos uma variável de ambiente: "
                        "ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY.",
                    )
                    return False
                
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

                if not user_input:
                    import sys
                    sys.stdout.write("\033[A\033[2K\r")
                    sys.stdout.flush()
                    continue
                await asyncio.sleep(0.5)
                if user_input.lower() in ['exit', 'quit', 'q']:
                    self.ui.display_message(UIMessage(content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]", message_type=MessageType.SYSTEM))
                    break

                with self.ui.show_loading("Processando sua solicitação..."):
                    response = await self.agent.process_input(
                        user_input=user_input,
                        session_id=self.default_session.session_id
                    )
                
                # Surface structured-error responses with a Rich panel instead of a plain
                # text blob. The agent sets explicit metadata flags for these cases.
                meta = response.metadata or {}
                if meta.get("budget_exceeded"):
                    from rich.panel import Panel
                    from rich.text import Text
                    self.ui.console.print(
                        Panel(
                            Text(f"{response.content}", style="yellow"),
                            title="[bold red]Budget Limit Reached[/bold red]",
                            border_style="red",
                            subtitle=(
                                f"provider={meta.get('provider_id', 'n/a')} • "
                                f"limit={meta.get('limit_type', 'n/a')}"
                            ),
                        )
                    )
                elif meta.get("forced_model_not_registered"):
                    from rich.panel import Panel
                    from rich.text import Text
                    self.ui.console.print(
                        Panel(
                            Text(f"{response.content}", style="yellow"),
                            title="[bold red]Forced Model Not Registered[/bold red]",
                            border_style="red",
                            subtitle="Use /model use auto to clear the override",
                        )
                    )
                else:
                    # Com Chat Sessions, tool executions estão integradas na resposta conversacional
                    self.ui.display_response(response.content, {
                        "execution_time": response.execution_time,
                        "model_used": response.metadata.get("model_used"),
                    })
                
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

async def _run_oneshot(message: str, forced_model: Optional[str] = None) -> int:
    """Run a single non-interactive turn. Prints response.content to stdout.

    Returns process exit code: 0 on success, 1 on any failure path. All diagnostic
    text is written to stderr so stdout stays clean for piping.
    """
    settings = get_settings()
    config_manager = ConfigManager()
    config_manager.load_config()

    model_router = get_model_router()
    if _use_legacy_gemini_only():
        registered = _bootstrap_legacy_gemini(model_router)
    else:
        registered = bootstrap_providers(router=model_router)
    if not registered:
        print(
            "ERROR: no provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "DEEPSEEK_API_KEY, or GOOGLE_API_KEY.",
            file=sys.stderr,
        )
        return 1

    agent = DeileAgent(
        model_router=model_router,
        tool_registry=get_tool_registry(),
        parser_registry=get_parser_registry(),
        config_manager=config_manager,
    )
    await agent.initialize()

    session = agent.create_session(
        session_id="oneshot_cli_session",
        working_directory=settings.working_directory,
    )
    if forced_model:
        session.context_data["forced_model"] = forced_model

    try:
        response = await agent.process_input(
            user_input=message,
            session_id=session.session_id,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(response.content)

    status = response.status.value if hasattr(response.status, "value") else str(response.status)
    return 0 if status != "error" else 1


def main():
    """Ponto de entrada principal da aplicação CLI."""
    logging.disable()

    if len(sys.argv) <= 1:
        cli = DeileAgentCLI()
        asyncio.run(cli.run_interactive())
        return

    parser = argparse.ArgumentParser(
        prog="deile",
        description="DEILE — Run interactively (no args) or send a single message and exit.",
    )
    parser.add_argument(
        "--model",
        dest="model",
        metavar="PROVIDER:MODEL_ID",
        help="Force a specific model (e.g. deepseek:deepseek-v4-flash). "
             "If omitted, uses default_model from api_config.yaml.",
    )
    parser.add_argument(
        "message",
        nargs=argparse.REMAINDER,
        help="Message to send to the agent. Quote it if it contains shell metacharacters.",
    )
    args = parser.parse_args()

    msg = " ".join(args.message).strip()
    if not msg and not sys.stdin.isatty():
        msg = sys.stdin.read().strip()

    if not msg:
        parser.error("no message provided (pass as positional args or via stdin)")

    exit_code = asyncio.run(_run_oneshot(msg, forced_model=args.model))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()