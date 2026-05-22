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
# Versão lite (pré-bootstrap, zero dependências)
# -----------------------------------------------------------------------------

def _parse_version_text() -> tuple[str, str]:
    """Extrai __version__ e __build_number__ de deile/__version__.py via parsing textual.

    Zero dependências externas — usa apenas str.split(). Importar o módulo
    não funcionaria porque deile/__init__.py pode puxar imports que dependem
    do venv.
    """
    version_file = PROJECT_ROOT / "deile" / "__version__.py"
    try:
        text = version_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "unknown", "unknown"
    version = "unknown"
    build = "unknown"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__") and "=" in stripped:
            version = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        elif stripped.startswith("__build_number__") and "=" in stripped:
            build = stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return version, build


def _print_lite_version() -> None:
    """Imprime versão minimalista — uma linha, zero deps."""
    version, build = _parse_version_text()
    print(f"DEILE v{version} (build {build})")


def _print_pre_bootstrap_help() -> None:
    """Imprime ajuda pré-bootstrap (sem venv)."""
    print("DEILE — Development Environment Intelligence & Learning Engine")
    print()
    print("Usage:")
    print("  python3 deile.py                   # interactive mode")
    print("  python3 deile.py \"your message\"    # one-shot mode")
    print("  python3 deile.py --version, -v      # show version")
    print("  python3 deile.py --help, -h         # show this help")
    print("  python3 deile.py --install          # install DEILE globally")
    print()
    print("Tip: run `python3 deile.py --version` to check the installed version.")
    print("     Use `deile --help` for the full command catalog (requires venv).")


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


def _lite_bootstrap() -> None:
    """Bootstrap leve: venv + deps (sem wizard de API keys). Re-executa no venv.

    Usado pelo caminho --version quando o usuário pede a versão full.
    Como --version não precisa de provider, não faz sentido pedir chaves.
    """
    _step("Instalando dependências para versão completa...")
    print()
    _check_python_version()
    _create_venv()
    _install_deps()
    print()
    _ok("Dependências instaladas. Obtendo versão completa...")
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

def main() -> None:
    # ── Pre-bootstrap flags: --version / --help (no venv needed) ──
    # Issue #270: these must work even on a fresh clone with no .venv/.
    args = sys.argv[1:]

    if "--version" in args or "-v" in args:
        _print_lite_version()
        if not _venv_python().exists() or not DEPS_MARKER.exists():
            # No venv yet (or venv exists but deps were never installed) —
            # offer full version (requires bootstrap).
            if sys.stdin.isatty():
                print()
                print(f"  {_DIM}A versão completa (com métricas e ambiente) requer")
                print(f"  a instalação das dependências (~1 min na primeira vez).{_RESET}")
                print()
                try:
                    ans = input("  Mostrar versão completa? [y/N]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    ans = "n"
                print()
                if ans in ("y", "yes"):
                    _lite_bootstrap()
            # Non-TTY or user declined: just print lite version and exit.
        else:
            # Venv exists with deps — re-execute to get the Rich panel via VersionCommand.
            _exec_in_venv()
        sys.exit(0)

    if "--help" in args or "-h" in args:
        _print_pre_bootstrap_help()
        sys.exit(0)

    # --install must run in the current interpreter (no venv redirect),
    # so the installation target matches what the user invoked.
    if "--install" in args:
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
