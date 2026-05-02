#!/usr/bin/env python3
"""DEILE — entry point com bootstrap embutido.

Comportamento:
  • Se `.venv/` não existe: roda o instalador completo (cria venv,
    instala dependências, pede chaves de API, grava `.env`) e re-executa
    dentro do venv.
  • Se `.venv/` existe e estamos fora dele: re-executa silenciosamente
    no python do venv.
  • Se já estamos dentro do venv: sobe o agente direto.

Uso:
    python3 deile.py                         # modo interativo
    python3 deile.py "sua mensagem"          # modo one-shot
    python3 deile.py --model PROVIDER:ID "msg"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_ROOT / ".venv"
ENV_FILE = PROJECT_ROOT / ".env"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
DEPS_MARKER = VENV_DIR / ".deile-deps-installed"
MIN_PYTHON = (3, 9)


# -----------------------------------------------------------------------------
# UI helpers (rodam com o python do sistema, antes do venv existir)
# -----------------------------------------------------------------------------

_TTY = sys.stdout.isatty()
_RESET = "\033[0m" if _TTY else ""
_BOLD = "\033[1m" if _TTY else ""
_DIM = "\033[2m" if _TTY else ""
_RED = "\033[0;31m" if _TTY else ""
_GREEN = "\033[0;32m" if _TTY else ""
_YELLOW = "\033[1;33m" if _TTY else ""
_BLUE = "\033[0;34m" if _TTY else ""
_CYAN = "\033[0;36m" if _TTY else ""


def _info(msg: str) -> None: print(f"  {_CYAN}ℹ{_RESET}  {msg}")
def _ok(msg: str)   -> None: print(f"  {_GREEN}✓{_RESET}  {msg}")
def _warn(msg: str) -> None: print(f"  {_YELLOW}⚠{_RESET}  {msg}")
def _err(msg: str)  -> None: print(f"  {_RED}✗{_RESET}  {msg}", file=sys.stderr)
def _step(msg: str) -> None: print(f"\n{_BOLD}{_BLUE}▶ {msg}{_RESET}")


def _banner() -> None:
    if _TTY:
        os.system("cls" if os.name == "nt" else "clear")
    print(
        f"{_BOLD}{_CYAN}\n"
        "  ╔════════════════════════════════════════════════════╗\n"
        "  ║                                                    ║\n"
        "  ║    🤖   D E I L E   —   bootstrap installer        ║\n"
        "  ║                                                    ║\n"
        "  ║    Development Environment Intelligence            ║\n"
        "  ║    & Learning Engine                               ║\n"
        "  ║                                                    ║\n"
        "  ╚════════════════════════════════════════════════════╝\n"
        f"{_RESET}"
    )


# -----------------------------------------------------------------------------
# Detecção de venv
# -----------------------------------------------------------------------------

def _venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _running_in_project_venv() -> bool:
    try:
        return _venv_python().resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _exec_in_venv() -> None:
    """Substitui o processo atual pelo python do venv. Não retorna."""
    py = str(_venv_python())
    args = [py, str(Path(__file__).resolve()), *sys.argv[1:]]
    if os.name == "nt":
        import subprocess
        sys.exit(subprocess.run(args).returncode)
    os.execv(py, args)


# -----------------------------------------------------------------------------
# Etapas do bootstrap (só rodam quando .venv ainda não existe)
# -----------------------------------------------------------------------------

def _check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        _err(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ é necessário "
            f"(detectado {sys.version_info.major}.{sys.version_info.minor})."
        )
        _err("Atualize seu Python (https://www.python.org/downloads/) e re-execute.")
        sys.exit(1)


def _create_venv() -> None:
    import venv
    _step("Criando ambiente virtual (.venv)")
    _info(f"{sys.executable} -m venv {VENV_DIR}")
    try:
        venv.EnvBuilder(with_pip=True).create(str(VENV_DIR))
    except Exception as exc:
        _err(f"Falha ao criar .venv: {exc}")
        _err("Verifique se o módulo `venv` está disponível para seu Python.")
        _err("Em Debian/Ubuntu: sudo apt-get install python3-venv")
        sys.exit(1)
    _ok(".venv criado")


def _install_deps() -> None:
    import subprocess
    _step("Instalando dependências")
    if not REQUIREMENTS.exists():
        _warn(f"{REQUIREMENTS.name} não encontrado — pulando.")
        return

    if (
        DEPS_MARKER.exists()
        and DEPS_MARKER.stat().st_mtime >= REQUIREMENTS.stat().st_mtime
    ):
        _ok("Dependências já instaladas (requirements.txt sem mudanças).")
        return

    py = str(_venv_python())
    subprocess.run(
        [py, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    _info("pip install -r requirements.txt (pode demorar na primeira vez)...")
    if subprocess.run(
        [py, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]
    ).returncode != 0:
        _err("Falha ao instalar dependências.")
        sys.exit(1)
    DEPS_MARKER.touch()
    _ok("Dependências instaladas")


def _ensure_env_file() -> None:
    _step("Verificando .env")
    if ENV_FILE.exists():
        _ok(".env já existe")
        return

    import getpass
    from datetime import datetime

    _warn(".env não encontrado — vamos configurar agora.")
    print()
    print(f"  {_BOLD}Chaves de API{_RESET}")
    print(f"  {_DIM}─────────────{_RESET}")
    print(f"  Você precisa de {_BOLD}PELO MENOS UMA{_RESET} chave entre os 4 providers.")
    print(f"  {_DIM}Pressione ENTER (em branco) para pular qualquer chave.{_RESET}")
    print(f"  {_DIM}A digitação fica oculta por segurança.{_RESET}")
    print()

    fields = [
        ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        ("OPENAI_API_KEY   ", "OPENAI_API_KEY"),
        ("DEEPSEEK_API_KEY ", "DEEPSEEK_API_KEY"),
        ("GOOGLE_API_KEY   ", "GOOGLE_API_KEY"),
    ]
    keys: dict[str, str] = {}
    for label, name in fields:
        try:
            keys[name] = getpass.getpass(f"  {label}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            _err("Cancelado.")
            sys.exit(1)

    if not any(keys.values()):
        _err("Você não informou nenhuma chave. DEILE precisa de pelo menos uma para subir.")
        sys.exit(1)

    lines = [
        f"# Gerado por deile.py em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# Você pode preencher chaves vazias depois para ativar mais providers.",
    ]
    lines.extend(f"{k}={v}" for k, v in keys.items())
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass

    count = sum(1 for v in keys.values() if v)
    _ok(f".env criado com {count} chave(s) preenchida(s) — permissões 0600")


def _bootstrap_first_run() -> None:
    """Instalador de primeira execução. Re-executa dentro do venv ao final."""
    _banner()
    _check_python_version()
    _create_venv()
    _install_deps()
    _ensure_env_file()
    _step("Iniciando DEILE")
    print()
    _info(f"Dica: digite {_BOLD}/help{_RESET} dentro do prompt para listar os comandos.")
    print()
    _exec_in_venv()


# -----------------------------------------------------------------------------
# Startup do agente (só roda dentro do venv)
# -----------------------------------------------------------------------------

def _silence_genai_shutdown_noise() -> None:
    """Torna `google.genai.Client.__del__` defensivo.

    No teardown do interpretador (especialmente em Python 3.14+), a finalização
    do SDK roda `Client.__del__` → `close()` → `self._api_client.close()` depois
    que `_api_client` já pode ter sumido pela ordem de GC dos módulos, gerando
    'Exception ignored ... AttributeError' no stderr. É puramente barulho de
    shutdown — não há nada para fechar a essa altura, pois o SO já libera os
    sockets — então swallowamos a exceção dentro do finalizer.
    """
    try:
        from google.genai import client as _gc  # noqa: WPS433 (import dentro de func)
    except ImportError:
        return
    original_del = _gc.Client.__del__

    def _safe_del(self) -> None:
        try:
            original_del(self)
        except Exception:
            pass

    _gc.Client.__del__ = _safe_del


def _start_deile() -> None:
    import argparse
    import asyncio
    import logging
    from typing import List, Optional

    sys.path.insert(0, str(PROJECT_ROOT))

    try:
        from dotenv import load_dotenv
        if ENV_FILE.exists():
            load_dotenv(ENV_FILE)
    except ImportError:
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
    except ImportError as exc:
        print(f"❌ ERRO: Falha ao importar módulos do DEILE: {exc}", file=sys.stderr)
        print("\nExecute deile.py a partir do diretório raiz do projeto.", file=sys.stderr)
        sys.exit(1)

    _silence_genai_shutdown_noise()

    model_providers_yaml = PROJECT_ROOT / "deile" / "config" / "model_providers.yaml"

    def use_legacy_gemini_only() -> bool:
        """Lê feature_flags.use_legacy_gemini_only de model_providers.yaml."""
        try:
            import yaml
            with open(model_providers_yaml) as f:
                data = yaml.safe_load(f)
            return bool(data.get("feature_flags", {}).get("use_legacy_gemini_only", False))
        except Exception:
            return False

    def bootstrap_legacy_gemini(router) -> list:
        if not os.getenv("GOOGLE_API_KEY"):
            return []
        try:
            from deile.core.models.gemini_provider import GeminiProvider
            router.register_provider(GeminiProvider(), priority=1)
            return ["gemini"]
        except Exception as exc:
            logging.getLogger(__name__).error("Legacy Gemini bootstrap failed: %s", exc)
            return []

    class DeileAgentCLI:
        """Interface de linha de comando do agente."""

        def __init__(self) -> None:
            self.settings = get_settings()
            self.logger = get_logger()
            self.config_manager = ConfigManager()
            self.ui = ConsoleUIManager(UITheme.DEFAULT, config_manager=self.config_manager)
            self.agent = None

        async def initialize(self) -> bool:
            try:
                self.ui.initialize()
                self.config_manager.load_config()

                with self.ui.show_loading("Inicializando DEILE v5.1..."):
                    model_router = get_model_router()

                    if use_legacy_gemini_only():
                        registered = bootstrap_legacy_gemini(model_router)
                    else:
                        registered = bootstrap_providers(router=model_router)
                    if not registered:
                        self.ui.display_error(
                            "Nenhum provider configurado.",
                            "Defina ao menos uma variável de ambiente: "
                            "ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY.",
                        )
                        return False

                    self.agent = DeileAgent(
                        model_router=model_router,
                        tool_registry=get_tool_registry(),
                        parser_registry=get_parser_registry(),
                        config_manager=self.config_manager,
                    )
                    await self.agent.initialize()

                    self.default_session = self.agent.create_session(
                        session_id="default_cli_session",
                        working_directory=self.settings.working_directory,
                    )

                self.ui.setup_hybrid_completion(working_directory=str(self.settings.working_directory))
                self.ui.setup_file_completion(self._get_project_files())
                return True

            except Exception as exc:
                self.ui.display_error(f"Falha fatal na inicialização do agente: {exc}")
                self.logger.error(f"Inicialização do agente falhou: {exc}", exc_info=True)
                return False

        async def run_interactive(self) -> None:
            if not await self.initialize():
                return

            self.ui.show_welcome()

            try:
                while True:
                    user_input = await asyncio.to_thread(self.ui.get_user_input, "\n > ")
                    user_input = user_input.strip()

                    if not user_input:
                        sys.stdout.write("\033[A\033[2K\r")
                        sys.stdout.flush()
                        continue

                    if user_input.lower() in ("exit", "quit", "q"):
                        self.ui.display_message(UIMessage(
                            content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]",
                            message_type=MessageType.SYSTEM,
                        ))
                        break

                    streaming_enabled = getattr(self.settings, "streaming_enabled", True)
                    if streaming_enabled:
                        event_stream = self.agent.process_input_stream(
                            user_input=user_input,
                            session_id=self.default_session.session_id,
                        )
                        try:
                            await self.ui.display_streaming_turn(event_stream)
                        except KeyboardInterrupt:
                            self.ui.console.print("\n[yellow](turn interrupted)[/yellow]")
                        continue

                    with self.ui.show_loading("Processando sua solicitação..."):
                        response = await self.agent.process_input(
                            user_input=user_input,
                            session_id=self.default_session.session_id,
                        )

                    meta = response.metadata or {}
                    if meta.get("budget_exceeded"):
                        from rich.panel import Panel
                        from rich.text import Text
                        self.ui.console.print(Panel(
                            Text(f"{response.content}", style="yellow"),
                            title="[bold red]Budget Limit Reached[/bold red]",
                            border_style="red",
                            subtitle=(
                                f"provider={meta.get('provider_id', 'n/a')} • "
                                f"limit={meta.get('limit_type', 'n/a')}"
                            ),
                        ))
                    elif meta.get("forced_model_not_registered"):
                        from rich.panel import Panel
                        from rich.text import Text
                        self.ui.console.print(Panel(
                            Text(f"{response.content}", style="yellow"),
                            title="[bold red]Forced Model Not Registered[/bold red]",
                            border_style="red",
                            subtitle="Use /model use auto to clear the override",
                        ))
                    else:
                        self.ui.display_response(response.content, {
                            "execution_time": response.execution_time,
                            "model_used": response.metadata.get("model_used"),
                        })

                    if response.tool_results and getattr(self.settings, "show_tool_details", False):
                        self.ui.console.print("\n[dim]Tool executions:[/dim]")
                        for result in response.tool_results:
                            if result.metadata and "rich_display" in result.metadata:
                                self.ui.console.print(f"[dim]{result.metadata['rich_display']}[/dim]")
                            else:
                                icon = "[green]●[/green]" if result.is_success else "[red]●[/red]"
                                self.ui.console.print(f"[dim]{icon} {result.message}[/dim]")

            except (KeyboardInterrupt, EOFError):
                self.ui.display_message(UIMessage(
                    content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]",
                    message_type=MessageType.SYSTEM,
                ))
            except Exception as exc:
                self.ui.display_error(f"Ocorreu um erro fatal no loop principal: {exc}")
                self.logger.critical(f"Erro fatal no modo interativo: {exc}", exc_info=True)

        def _get_project_files(self) -> List[str]:
            files: List[str] = []
            working_dir = Path(self.settings.working_directory)
            ignore_dirs = {
                "__pycache__", ".git", "node_modules", ".venv", "venv",
                "requests", "logs", "dist", "build", ".deile",
            }
            for path in working_dir.rglob("*"):
                if path.is_file() and not any(d in path.parts for d in ignore_dirs):
                    rel = path.relative_to(working_dir)
                    files.append(str(rel).replace("\\", "/"))
            return sorted(files)[:500]

    async def run_oneshot(message: str, forced_model: Optional[str] = None) -> int:
        """Roda um único turno não-interativo. stdout = response.content."""
        settings = get_settings()
        config_manager = ConfigManager()
        config_manager.load_config()

        model_router = get_model_router()
        if use_legacy_gemini_only():
            registered = bootstrap_legacy_gemini(model_router)
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

    logging.disable()

    if len(sys.argv) <= 1:
        asyncio.run(DeileAgentCLI().run_interactive())
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

    sys.exit(asyncio.run(run_oneshot(msg, forced_model=args.model)))


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    if _running_in_project_venv():
        _start_deile()
        return
    if _venv_python().exists():
        _exec_in_venv()
    else:
        _bootstrap_first_run()


if __name__ == "__main__":
    main()
