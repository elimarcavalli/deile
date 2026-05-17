#!/usr/bin/env python3
"""Instalador de ambiente do ecossistema DEILE / deilebot.

Responsabilidade única: **preparar a máquina**. Não configura o bot
(isso é o `deilebot setup`) nem sobe a stack (isso é o `deploy.py`).

O que ele garante:
  1. Python 3.9+.
  2. O repositório `deilebot` clonado dentro de `deile/`.
  3. As dependências Python instaladas (`pip install -e .` + o bot).
  4. (Modo container) um runtime de container + Kubernetes:
       - Linux  : k3s (script oficial get.k3s.io).
       - macOS  : reusa o Rancher Desktop se presente; senão, colima
                  via Homebrew (instala o Homebrew se faltar).
       - Windows: instruções guiadas (não há auto-instalação limpa).

Uso:
    python3 infra/setup_environment.py            # interativo
    python3 infra/setup_environment.py --check    # só diagnostica
    python3 infra/setup_environment.py --yes      # não pergunta nada
    python3 infra/setup_environment.py --mode container

Apenas stdlib — roda numa máquina limpa, antes de qualquer `pip install`.
Cada ação de instalação é mostrada e confirmada antes de rodar; com
`--check` nada é instalado.
"""

from __future__ import annotations

import argparse
import importlib.util
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli_ui as ui  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEILEBOT_REPO = "https://github.com/elimarcavalli/deilebot.git"
K3S_INSTALL_URL = "https://get.k3s.io"
HOMEBREW_INSTALL_URL = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)


class OSInfo:
    """Sistema operacional detectado."""

    def __init__(self) -> None:
        self.system = platform.system()          # Linux | Darwin | Windows
        self.arch = platform.machine()           # x86_64 | arm64 | ...
        self.is_linux = self.system == "Linux"
        self.is_macos = self.system == "Darwin"
        self.is_windows = self.system == "Windows"

    @property
    def label(self) -> str:
        return f"{self.system} ({self.arch})"


# ----- helpers de execução --------------------------------------------------

def _have(cmd: str) -> bool:
    """True se ``cmd`` está no PATH."""
    return shutil.which(cmd) is not None


def _rancher_desktop_kubectl() -> Optional[Path]:
    """Caminho do kubectl do Rancher Desktop, se instalado."""
    candidate = Path.home() / ".rd" / "bin" / "kubectl"
    return candidate if candidate.is_file() else None


def _run_quiet(cmd: List[str], timeout: float = 15.0) -> Optional[int]:
    """Roda um comando silenciosamente; devolve o exit code (ou None)."""
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def _confirm_and_run(
    description: str, cmd, *, yes: bool, check_mode: bool, shell: bool = False
) -> bool:
    """Mostra o comando, confirma e executa. True em caso de sucesso."""
    shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    ui.command("$ ", shown)
    if check_mode:
        ui.info(f"[--check] não executado: {description}")
        return False
    if not yes and not ui.confirm(f"Executar — {description}?", default=True):
        ui.warn("Pulado pelo operador.")
        return False
    try:
        proc = subprocess.run(cmd, shell=shell, cwd=str(ROOT))
    except OSError as exc:
        ui.err(f"não consegui executar: {exc}")
        return False
    if proc.returncode != 0:
        ui.err(f"o comando saiu com código {proc.returncode}")
        return False
    ui.ok(f"{description} — concluído.")
    return True


# ----- checagens -------------------------------------------------------------

def check_python() -> bool:
    """Confere Python 3.9+. Não há auto-fix — só reporta."""
    v = sys.version_info
    if v >= (3, 9):
        ui.ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    ui.err(
        f"Python {v.major}.{v.minor} é antigo demais — o DEILE precisa de "
        "3.9+. Instale uma versão mais nova e rode este script de novo."
    )
    return False


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def deilebot_cloned() -> bool:
    return (ROOT / "deilebot" / "pyproject.toml").is_file()


def missing_python_deps() -> List[str]:
    """Lista os módulos-chave que ainda não estão importáveis."""
    missing = []
    for mod in ("deile", "deilebot", "discord"):
        if not _module_available(mod):
            missing.append(mod)
    return missing


def kubernetes_ready() -> bool:
    """True se há um kubectl funcional apontando para um cluster vivo."""
    kubectl = "kubectl" if _have("kubectl") else None
    if kubectl is None:
        rd = _rancher_desktop_kubectl()
        if rd is None:
            return False
        kubectl = str(rd)
    return _run_quiet([kubectl, "cluster-info"], timeout=20.0) == 0


# ----- instaladores ----------------------------------------------------------

def ensure_deilebot_clone(yes: bool, check_mode: bool) -> bool:
    if deilebot_cloned():
        ui.ok("repositório `deilebot` presente")
        return True
    ui.warn("o repositório `deilebot` não está clonado dentro de `deile/`")
    return _confirm_and_run(
        "clonar o deilebot",
        ["git", "clone", DEILEBOT_REPO, "deilebot"],
        yes=yes, check_mode=check_mode,
    )


def install_python_deps(yes: bool, check_mode: bool) -> bool:
    """Instala o agente core e o bot em modo editável."""
    pip = [sys.executable, "-m", "pip", "install", "-e"]
    okk = _confirm_and_run(
        "instalar o agente DEILE (core)", pip + ["."],
        yes=yes, check_mode=check_mode,
    )
    if not okk:
        return False
    return _confirm_and_run(
        "instalar o deilebot + discord.py", pip + ["./deilebot[discord]"],
        yes=yes, check_mode=check_mode,
    )


def install_kubernetes_linux(yes: bool, check_mode: bool) -> bool:
    """Instala o k3s (Kubernetes leve) — traz containerd + kubectl embutidos."""
    ui.info(
        "No Linux o caminho recomendado é o k3s — um Kubernetes leve, do "
        "mesmo projeto do Rancher. O script oficial precisa de sudo."
    )
    okk = _confirm_and_run(
        "instalar o k3s (get.k3s.io)",
        f"curl -sfL {K3S_INSTALL_URL} | sh -",
        yes=yes, check_mode=check_mode, shell=True,
    )
    if not okk:
        return False
    # O kubeconfig do k3s nasce root-only em /etc/rancher/k3s/k3s.yaml.
    # Copiá-lo para ~/.kube/config deixa o `kubectl` funcionar sem sudo.
    kube_dir = Path.home() / ".kube"
    ui.info(
        "O kubeconfig do k3s é root-only. Vou copiá-lo para ~/.kube/config "
        "para o kubectl funcionar sem sudo."
    )
    return _confirm_and_run(
        "copiar o kubeconfig do k3s para ~/.kube/config",
        f"mkdir -p {kube_dir} && sudo cp /etc/rancher/k3s/k3s.yaml "
        f"{kube_dir}/config && sudo chown $(id -u):$(id -g) {kube_dir}/config",
        yes=yes, check_mode=check_mode, shell=True,
    )


def install_kubernetes_macos(yes: bool, check_mode: bool) -> bool:
    """macOS: reusa o Rancher Desktop; senão instala o colima via Homebrew."""
    if _rancher_desktop_kubectl() is not None:
        ui.ok("Rancher Desktop detectado — vou usá-lo")
        ui.info(
            "Abra o app Rancher Desktop e confirme que o Kubernetes está "
            "ligado (Preferences → Kubernetes)."
        )
        return True

    ui.info(
        "Sem Rancher Desktop — vou pelo colima: um runtime de container + "
        "k3s sem GUI, 100% por linha de comando."
    )
    if not _have("brew"):
        ui.warn("Homebrew não encontrado — ele é necessário para o colima")
        okk = _confirm_and_run(
            "instalar o Homebrew",
            f'/bin/bash -c "$(curl -fsSL {HOMEBREW_INSTALL_URL})"',
            yes=yes, check_mode=check_mode, shell=True,
        )
        if not okk:
            return False

    okk = _confirm_and_run(
        "instalar colima, kubectl e o cliente docker",
        ["brew", "install", "colima", "kubectl", "docker"],
        yes=yes, check_mode=check_mode,
    )
    if not okk:
        return False
    # `--runtime containerd` deixa o colima usar containerd (igual ao
    # Rancher Desktop), então `colima nerdctl build` alimenta o mesmo
    # containerd que o k3s lê — o deploy.py conta com isso.
    return _confirm_and_run(
        "iniciar o colima com Kubernetes",
        ["colima", "start", "--kubernetes", "--runtime", "containerd"],
        yes=yes, check_mode=check_mode,
    )


def guide_kubernetes_windows() -> None:
    """Windows não tem auto-instalação limpa — instruções guiadas."""
    ui.warn("No Windows não há auto-instalação limpa de Kubernetes.")
    ui.info("Instale UMA das opções abaixo e habilite o Kubernetes nela:")
    ui.detail("• Rancher Desktop — https://rancherdesktop.io/")
    ui.detail("• Docker Desktop  — https://www.docker.com/products/docker-desktop/")
    ui.detail("Depois rode este script de novo para validar.")


# ----- orquestração ----------------------------------------------------------

def _wants_container(args: argparse.Namespace) -> bool:
    if args.mode == "container":
        return True
    if args.mode == "local":
        return False
    if args.yes:
        return False  # default não-interativo: só ambiente local
    return ui.confirm(
        "Você vai rodar o bot em container (Kubernetes)?", default=False
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_environment",
        description="Prepara a máquina para rodar o deilebot / DEILE.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Só diagnostica — não instala nada",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Assume 'sim' em todas as confirmações (não-interativo)",
    )
    parser.add_argument(
        "--mode", choices=["local", "container"], default=None,
        help="Pula a pergunta sobre usar Kubernetes",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Desliga as cores",
    )
    args = parser.parse_args(argv)
    if args.no_color:
        ui.set_color(False)

    check_mode = args.check
    osinfo = OSInfo()

    ui.header("DEILE — preparação do ambiente")
    ui.info(f"Sistema: {osinfo.label}")
    if check_mode:
        ui.info("Modo --check: apenas diagnóstico, nada será instalado.")

    issues: List[str] = []

    # 1. Python
    ui.section("1 · Python")
    if not check_python():
        # Sem Python adequado não dá para seguir.
        ui.err("Pré-requisito não atendido. Abortando.")
        return 1

    # 2. Repositório do bot
    ui.section("2 · Repositório do bot")
    if deilebot_cloned():
        ui.ok("repositório `deilebot` presente")
    elif check_mode:
        ui.warn("repositório `deilebot` ausente")
        issues.append("deilebot não clonado")
    elif not ensure_deilebot_clone(args.yes, check_mode):
        issues.append("deilebot não clonado")

    # 3. Dependências Python
    ui.section("3 · Dependências Python")
    missing = missing_python_deps()
    if not missing:
        ui.ok("deile, deilebot e discord.py instalados")
    elif check_mode:
        ui.warn(f"faltando: {', '.join(missing)}")
        issues.append("dependências Python")
    else:
        ui.info(f"faltando: {', '.join(missing)} — instalando...")
        if install_python_deps(args.yes, check_mode):
            if missing_python_deps():
                issues.append("dependências Python")
        else:
            issues.append("dependências Python")

    # 4. Kubernetes (apenas se for usar container)
    if _wants_container(args):
        ui.section("4 · Container + Kubernetes")
        if kubernetes_ready():
            ui.ok("Kubernetes acessível (cluster respondendo)")
        elif check_mode:
            ui.warn("nenhum cluster Kubernetes acessível")
            issues.append("Kubernetes")
        else:
            installed = False
            if osinfo.is_linux:
                installed = install_kubernetes_linux(args.yes, check_mode)
            elif osinfo.is_macos:
                installed = install_kubernetes_macos(args.yes, check_mode)
            else:
                guide_kubernetes_windows()
            if not installed or not kubernetes_ready():
                issues.append("Kubernetes")
    else:
        ui.section("4 · Container + Kubernetes")
        ui.info("modo local — Kubernetes não é necessário; pulando.")

    # Resumo
    ui.section("Resumo")
    if not issues:
        ui.ok("Ambiente pronto.")
        ui.plain()
        ui.info("Próximo passo — configure o bot:")
        ui.command("", "python -m deilebot setup")
        return 0

    for item in issues:
        ui.err(f"pendência: {item}")
    ui.plain()
    if check_mode:
        ui.info("Rode sem --check para instalar as pendências.")
    else:
        ui.info("Resolva as pendências acima e rode o script novamente.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
