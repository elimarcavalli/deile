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
    python3 deile.py --install               # auto-instala globalmente (user)
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
_API_KEY_NAMES = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY"})


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


def _env_file_has_valid_key() -> bool:
    """Return True if ENV_FILE exists and has at least one non-empty recognized API key."""
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() in _API_KEY_NAMES and v.strip():
            return True
    return False


def _ensure_env_file() -> None:
    _step("Verificando .env")
    if _env_file_has_valid_key():
        _ok(".env existe com pelo menos uma chave configurada")
        return

    import getpass
    from datetime import datetime

    if ENV_FILE.exists():
        _warn(".env existe mas nenhuma chave de API está preenchida — vamos configurar agora.")
    else:
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
    """Delegates to deile.cli.main() for the actual agent startup."""
    import sys as _sys
    _sys.path.insert(0, str(PROJECT_ROOT))
    from deile.cli import main as _cli_main
    _sys.exit(_cli_main())


# Entry point
# -----------------------------------------------------------------------------

def _handle_preflight_flag() -> bool:
    """Handle --version / --help BEFORE any venv/bootstrap.

    Returns True if a preflight flag was handled (caller should exit),
    False otherwise (proceed with normal startup).
    """
    argv = sys.argv[1:]

    if "--version" in argv:
        _print_version()
        return True

    if "--help" in argv or "-h" in argv:
        _print_help()
        return True

    return False


def _print_version() -> None:
    """Print DEILE version from deile/__version__.py (zero external deps)."""
    import importlib.util

    version_path = PROJECT_ROOT / "deile" / "__version__.py"
    spec = importlib.util.spec_from_file_location(
        "_deile_version", str(version_path)
    )
    if spec is None or spec.loader is None:
        print("DEILE (version unknown)", file=sys.stderr)
        sys.exit(1)

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"DEILE (version unknown: {exc})", file=sys.stderr)
        sys.exit(1)

    version = getattr(mod, "__version__", "unknown")
    build = getattr(mod, "__build_number__", "unknown")
    print(f"DEILE v{version} (build {build})")


def _print_help() -> None:
    """Print basic usage help (zero external deps)."""
    print(
        f"{_BOLD}DEILE{_RESET} \u2014 Development Environment Intelligence & Learning Engine\n"
        f"\n"
        f"Uso:\n"
        f"  python3 deile.py                  # modo interativo\n"
        f"  python3 deile.py \"mensagem\"       # modo one-shot\n"
        f"  python3 deile.py --version         # exibe a vers\u00e3o\n"
        f"  python3 deile.py --help            # esta ajuda\n"
        f"  python3 deile.py --install         # instala globalmente (user)\n"
        f"  python3 deile.py --model PROVIDER:ID \"msg\"\n"
        f"\n"
        f"Reposit\u00f3rio: https://github.com/elimarcavalli/deile\n"
    )


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    # Preflight flags that need NO venv, NO provider, NO bootstrap.
    if _handle_preflight_flag():
        sys.exit(0)

    # --install must run in the current interpreter (no venv redirect),
    # so the installation target matches what the user invoked.
    if "--install" in sys.argv[1:]:
        _start_deile()
        return

    if _running_in_project_venv():
        _start_deile()
        return
    if _venv_python().exists():
        _exec_in_venv()
    else:
        _bootstrap_first_run()


if __name__ == "__main__":
    main()
