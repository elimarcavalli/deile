#!/usr/bin/env python3
"""Orquestrador do ciclo de vida do deilebot / DEILE.

Substitui o antigo `run.sh`. Sobe, para, reinicia e inspeciona o bot —
tanto no modo **container** (Kubernetes) quanto no modo **local** (o bot
como serviço de segundo plano numa máquina sem k8s).

    python3 infra/k8s/deploy.py help          # lista todos os comandos
    python3 infra/k8s/deploy.py doctor        # diagnostica os pré-requisitos
    python3 infra/k8s/deploy.py up            # sobe a stack no Kubernetes
    python3 infra/k8s/deploy.py stop          # fecha o bot
    python3 infra/k8s/deploy.py reset         # reset completo

O alvo (local/container) vem de `.deile/deploy.json` (gravado pelo
`deilebot setup`); use `--target local|container` para forçar.

Antes de qualquer operação de container, os pré-requisitos são checados;
se faltar Kubernetes, o `setup_environment.py` é oferecido.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from shutil import which
from typing import Dict, List, Optional

_INFRA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_INFRA))
import _cli_ui as ui  # noqa: E402
from _service import LocalService  # noqa: E402

HERE = Path(__file__).resolve().parent          # infra/k8s/
ROOT = _INFRA.parent                            # raiz do repo deile/
MANIFESTS = HERE / "manifests"
ENV_FILE = ROOT / ".env"
DEPLOY_STATE = ROOT / ".deile" / "deploy.json"
SETUP_ENV = _INFRA / "setup_environment.py"

NS = "deile"
IMAGE = "deile-stack:local"
LLM_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY")
K8S_DEPLOYMENTS = ("deilebot", "deile-worker", "deile-shell")


# ===== helpers ===============================================================

def _resolve(tool: str) -> Optional[str]:
    """Acha um binário no PATH ou no diretório do Rancher Desktop."""
    if which(tool):
        return tool
    rd = Path.home() / ".rd" / "bin" / tool
    return str(rd) if rd.is_file() else None


def _kubectl() -> Optional[str]:
    return _resolve("kubectl")


def _run(cmd: List[str], **kw) -> int:
    """Roda um comando streamando a saída; devolve o returncode."""
    try:
        return subprocess.run(cmd, cwd=str(ROOT), **kw).returncode
    except OSError as exc:
        ui.err(f"falha ao executar {cmd[0]}: {exc}")
        return 1


def _capture(cmd: List[str], timeout: float = 30.0) -> Optional[str]:
    """Roda um comando capturando stdout; None em caso de erro."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT)
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def read_env() -> Dict[str, str]:
    """Lê o `.env` da raiz como um dicionário simples."""
    data: Dict[str, str] = {}
    if not ENV_FILE.is_file():
        return data
    for raw in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        data[key.strip()] = val.strip().strip('"').strip("'")
    return data


def read_deploy_target() -> Optional[str]:
    """Lê o alvo gravado pelo wizard em `.deile/deploy.json`."""
    try:
        data = json.loads(DEPLOY_STATE.read_text(encoding="utf-8"))
        target = data.get("target")
        return target if target in ("local", "container") else None
    except (OSError, ValueError):
        return None


def write_deploy_target(target: str) -> None:
    DEPLOY_STATE.parent.mkdir(parents=True, exist_ok=True)
    DEPLOY_STATE.write_text(
        json.dumps({"target": target}, indent=2) + "\n", encoding="utf-8"
    )


def cluster_reachable() -> bool:
    kubectl = _kubectl()
    if kubectl is None:
        return False
    try:
        proc = subprocess.run(
            [kubectl, "cluster-info"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def namespace_exists() -> bool:
    kubectl = _kubectl()
    if kubectl is None:
        return False
    return _run(
        [kubectl, "get", "namespace", NS],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) == 0


def resolve_target(requested: Optional[str]) -> Optional[str]:
    """--target > .deile/deploy.json > auto-detecção."""
    if requested in ("local", "container"):
        return requested
    saved = read_deploy_target()
    if saved:
        return saved
    if namespace_exists():
        return "container"
    svc = LocalService(ROOT)
    running, _ = svc.status()
    if running:
        return "local"
    return None


# ===== pré-requisitos ========================================================

def ensure_container_prereqs(yes: bool) -> bool:
    """Garante kubectl + cluster vivo; oferece o setup_environment se faltar."""
    if cluster_reachable():
        return True
    ui.warn("nenhum cluster Kubernetes acessível.")
    if not SETUP_ENV.is_file():
        ui.err(f"instalador de ambiente não encontrado em {SETUP_ENV}")
        return False
    if not yes and not ui.confirm(
        "Rodar o instalador de ambiente agora?", default=True
    ):
        ui.info("Rode você mesmo: python3 infra/setup_environment.py --mode container")
        return False
    rc = _run([sys.executable, str(SETUP_ENV), "--mode", "container"]
              + (["--yes"] if yes else []))
    if rc != 0 or not cluster_reachable():
        ui.err("o ambiente ainda não está pronto.")
        return False
    return True


# ===== comandos de container =================================================

def _image_build_cmd() -> Optional[List[str]]:
    """Monta o comando de build conforme o runtime de container disponível."""
    dockerfile = str(HERE / "Dockerfile")
    nerdctl = _resolve("nerdctl")
    if nerdctl:
        # Rancher Desktop / containerd — k3s lê o namespace k8s.io.
        return [nerdctl, "--namespace", "k8s.io", "build",
                "-f", dockerfile, "-t", IMAGE, str(ROOT)]
    if which("colima"):
        return ["colima", "nerdctl", "--", "--namespace", "k8s.io", "build",
                "-f", dockerfile, "-t", IMAGE, str(ROOT)]
    if which("docker"):
        ui.warn("usando `docker build` — num cluster k3s a imagem pode "
                "precisar de import manual no containerd.")
        return ["docker", "build", "-f", dockerfile, "-t", IMAGE, str(ROOT)]
    return None


def cmd_build(args: dict) -> int:
    ui.section("Build da imagem")
    if not ensure_container_prereqs(args["yes"]):
        return 1
    build_cmd = _image_build_cmd()
    if build_cmd is None:
        ui.err("nenhum runtime de container encontrado (nerdctl/colima/docker).")
        ui.info("Rode: python3 infra/setup_environment.py --mode container")
        return 1
    ui.info(f"imagem: {IMAGE}")
    if _run(build_cmd) != 0:
        ui.err("o build falhou.")
        return 1
    ui.ok("imagem construída.")
    # imagePullPolicy: Never → um rebuild só vale após reiniciar os pods.
    kubectl = _kubectl()
    for dep in K8S_DEPLOYMENTS:
        if kubectl and _run([kubectl, "-n", NS, "get", "deployment", dep],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL) == 0:
            ui.info(f"reiniciando deployment/{dep} para pegar a nova imagem")
            _run([kubectl, "-n", NS, "rollout", "restart", f"deployment/{dep}"],
                 stdout=subprocess.DEVNULL)
    return 0


def _apply_secret(kubectl: str, name: str, kv: Dict[str, str]) -> bool:
    """Cria/atualiza um Secret a partir de pares chave=valor.

    Usa um arquivo temporário modo 0600 (apagado em seguida) — os valores
    nunca aparecem em argv (`ps`) nem ficam num Secret pela metade.
    """
    fd, tmp = tempfile.mkstemp(prefix="deile-secret-", suffix=".env")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for key, val in kv.items():
                fh.write(f"{key}={val}\n")
        rendered = _capture([
            kubectl, "-n", NS, "create", "secret", "generic", name,
            f"--from-env-file={tmp}", "--dry-run=client", "-o", "yaml",
        ])
        if rendered is None:
            ui.err(f"não consegui renderizar o Secret {name}")
            return False
        apply = subprocess.run(
            [kubectl, "apply", "-f", "-"],
            input=rendered, text=True, capture_output=True,
        )
        if apply.returncode != 0:
            ui.err(f"apply do Secret {name}: {apply.stderr.strip()}")
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cmd_up(args: dict) -> int:
    ui.section("Subir a stack (Kubernetes)")
    if not ensure_container_prereqs(args["yes"]):
        return 1
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1
    if not ENV_FILE.is_file():
        ui.err(f"`.env` não encontrado em {ENV_FILE} — rode `deilebot setup`.")
        return 1

    env = read_env()
    discord_token = env.get("DEILE_BOT_DISCORD_TOKEN", "").strip()
    if not discord_token:
        ui.err("DEILE_BOT_DISCORD_TOKEN ausente no `.env`.")
        return 1
    llm = {k: env[k] for k in LLM_KEYS if env.get(k, "").strip()}
    if not llm:
        ui.err("nenhuma chave de LLM no `.env` (ANTHROPIC/OPENAI/DEEPSEEK/GOOGLE).")
        return 1
    bearer = env.get("DEILE_BOT_AUTH_TOKEN", "").strip() or secrets.token_urlsafe(32)
    worker_token = (
        env.get("DEILE_WORKER_BEARER_TOKEN", "").strip() or secrets.token_urlsafe(32)
    )
    github_token = env.get("GITHUB_TOKEN", "").strip()

    ui.info("aplicando namespace e network policies")
    _run([kubectl, "apply", "-f", str(MANIFESTS / "00-namespace.yaml")])
    _run([kubectl, "apply", "-f", str(MANIFESTS / "40-network-policy.yaml")])

    ui.info("criando os Secrets (nada é impresso)")
    bot_secret = {"DEILE_BOT_DISCORD_TOKEN": discord_token,
                  "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN": bearer, **llm}
    deile_secret = {"DEILE_BOT_AUTH_TOKEN": bearer, **llm}
    if github_token:
        deile_secret["GITHUB_TOKEN"] = github_token
    for name, kv in (("bot-secrets", bot_secret),
                     ("deile-secrets", deile_secret),
                     ("worker-bearer", {"AUTH_TOKEN": worker_token})):
        if not _apply_secret(kubectl, name, kv):
            return 1

    ui.info("aplicando ConfigMap, PVCs, Deployments e Services")
    for manifest in ("15-bot-config.yaml", "19-bot-data-pvc.yaml",
                     "20-bot-deployment.yaml", "35-deile-interactive.yaml",
                     "41-worker-pvc.yaml", "45-deile-worker-deployment.yaml"):
        _run([kubectl, "apply", "-f", str(MANIFESTS / manifest)])

    for dep in K8S_DEPLOYMENTS:
        _run([kubectl, "-n", NS, "rollout", "restart", f"deployment/{dep}"],
             stdout=subprocess.DEVNULL)

    ui.info("aguardando os pods ficarem prontos (até 180s cada)")
    for dep in K8S_DEPLOYMENTS:
        if _run([kubectl, "-n", NS, "rollout", "status",
                 f"deployment/{dep}", "--timeout=180s"]) != 0:
            ui.err(f"{dep} não ficou pronto.")
            _run([kubectl, "-n", NS, "logs", f"deploy/{dep}", "--tail=60"])
            return 1
    ui.ok("stack no ar.")
    return 0


def cmd_down(args: dict) -> int:
    ui.section("Teardown completo (Kubernetes)")
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1
    ui.warn(f"isto APAGA o namespace `{NS}` inteiro: pods, Secrets, PVCs e os "
            "dados persistidos (histórico, cron, sessões).")
    if not args["yes"] and not ui.confirm("Confirmar o teardown?", default=False):
        ui.info("Cancelado.")
        return 1
    rc = _run([kubectl, "delete", "namespace", NS, "--ignore-not-found"])
    if rc == 0:
        ui.ok("namespace removido.")
    return rc


def cmd_test(args: dict) -> int:
    ui.section("Job one-shot do DEILE")
    kubectl = _kubectl()
    if kubectl is None or not cluster_reachable():
        ui.err("cluster Kubernetes inacessível.")
        return 1
    _run([kubectl, "-n", NS, "delete", "job", "deile-oneshot", "--ignore-not-found"],
         stdout=subprocess.DEVNULL)
    _run([kubectl, "apply", "-f", str(MANIFESTS / "30-deile-job.yaml")])
    pod = (_capture([kubectl, "-n", NS, "get", "pods", "-l", "job-name=deile-oneshot",
                     "-o", "jsonpath={.items[0].metadata.name}"]) or "").strip()
    if not pod:
        time.sleep(3)
        pod = (_capture([kubectl, "-n", NS, "get", "pods",
                         "-l", "job-name=deile-oneshot",
                         "-o", "jsonpath={.items[0].metadata.name}"]) or "").strip()
    if not pod:
        ui.err("o pod do Job não apareceu.")
        return 1
    ui.info("streamando os logs do Job (Ctrl-C para o stream, não o Job)")
    _run([kubectl, "-n", NS, "logs", "--pod-running-timeout=120s", "-f", pod])
    return 0


_CLONE_SNIPPET = r'''
import os, subprocess, sys
from pathlib import Path

clone_url = sys.argv[1]
work_dir  = sys.argv[2]

home = Path(os.environ.get("HOME", "/home/deile"))
token_file = Path("/run/secrets/deile/GITHUB_TOKEN")
if token_file.exists():
    token = token_file.read_text().strip()
    creds = home / ".git-credentials"
    fd = os.open(str(creds), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("https://oauth2:" + token + "@github.com\n")
    subprocess.run(["git", "config", "--global", "credential.helper", "store"],
                   check=False)

(home / "work").mkdir(parents=True, exist_ok=True)
git_bin = home / "bin" / "git"
# Fail-closed: a allowlist de clone é enforçada EXCLUSIVAMENTE pelo guard
# ~/bin/git. Sem ele, cair para /usr/bin/git clonaria qualquer URL sem
# validação — o que contradiz o modelo de segurança. Recusamos o clone.
if not git_bin.exists():
    print("ERRO: guard de clone (~/bin/git) não instalado — a allowlist "
          "de repositórios não pode ser aplicada. Clone RECUSADO por "
          "segurança. Verifique o wrapper.py do deile-shell.",
          file=sys.stderr)
    sys.exit(1)

result = subprocess.run(
    [str(git_bin), "clone", "--depth", "1", clone_url, work_dir],
    env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
         "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
)
sys.exit(result.returncode)
'''


def cmd_clone(args: dict) -> int:
    ui.section("Clonar repositório no deile-shell")
    if not args["extra"]:
        ui.err("uso: deploy.py clone <owner/repo>")
        return 1
    repo = args["extra"][0]
    kubectl = _kubectl()
    if kubectl is None or not namespace_exists():
        ui.err("namespace não encontrado — rode `up` primeiro.")
        return 1
    env = read_env()
    github_token = env.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        ui.err("GITHUB_TOKEN ausente no `.env` (token de leitura/clone).")
        return 1
    llm = {k: env[k] for k in LLM_KEYS if env.get(k, "").strip()}
    bearer = env.get("DEILE_BOT_AUTH_TOKEN", "").strip()
    if not bearer:
        ui.err("DEILE_BOT_AUTH_TOKEN ausente — rode `up` primeiro.")
        return 1

    ui.info("injetando o GITHUB_TOKEN no Secret deile-secrets")
    if not _apply_secret(kubectl, "deile-secrets",
                         {"DEILE_BOT_AUTH_TOKEN": bearer,
                          "GITHUB_TOKEN": github_token, **llm}):
        return 1

    ui.info("aguardando o kubelet sincronizar o token no pod (até 90s)")
    deadline = time.time() + 90
    synced = False
    while time.time() < deadline:
        if _run([kubectl, "-n", NS, "exec", "deploy/deile-shell", "--",
                 "test", "-f", "/run/secrets/deile/GITHUB_TOKEN"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            synced = True
            break
        time.sleep(10)
    if not synced:
        ui.err("o token não sincronizou no pod em 90s.")
        return 1

    name = repo.rstrip("/").split("/")[-1]
    clone_url = f"https://github.com/{repo}.git"
    work_dir = f"/home/deile/work/{name}"
    ui.info(f"clonando {clone_url} → {work_dir}")
    rc = _run([kubectl, "-n", NS, "exec", "deploy/deile-shell", "--",
               "python3", "-c", _CLONE_SNIPPET, clone_url, work_dir])
    if rc == 0:
        ui.ok(f"repo disponível em {work_dir} (dentro do deile-shell).")
    return rc


# ===== ciclo de vida (sensível ao modo) ======================================

def cmd_start(args: dict) -> int:
    target = resolve_target(args["target"])
    ui.section("Iniciar")
    if target == "local":
        return 0 if LocalService(ROOT).start() else 1
    if target == "container":
        kubectl = _kubectl()
        if kubectl and namespace_exists():
            ui.info("religando os deployments (scale → 1)")
            for dep in K8S_DEPLOYMENTS:
                _run([kubectl, "-n", NS, "scale", f"deployment/{dep}",
                      "--replicas=1"], stdout=subprocess.DEVNULL)
            ui.ok("deployments religados.")
            return 0
        return cmd_up(args)
    ui.err("alvo indefinido — rode `deilebot setup` ou use --target local|container.")
    return 1


def cmd_stop(args: dict) -> int:
    target = resolve_target(args["target"])
    ui.section("Parar (fechar o bot)")
    if target == "local":
        return 0 if LocalService(ROOT).stop() else 1
    if target == "container":
        kubectl = _kubectl()
        if kubectl is None or not namespace_exists():
            ui.warn("nada para parar (namespace ausente).")
            return 0
        ui.info("escalando os deployments para 0 (dados e Secrets ficam)")
        for dep in K8S_DEPLOYMENTS:
            _run([kubectl, "-n", NS, "scale", f"deployment/{dep}",
                  "--replicas=0"], stdout=subprocess.DEVNULL)
        ui.ok("bot parado.")
        return 0
    ui.err("alvo indefinido — use --target local|container.")
    return 1


def cmd_restart(args: dict) -> int:
    target = resolve_target(args["target"])
    ui.section("Reiniciar")
    if target == "local":
        return 0 if LocalService(ROOT).restart() else 1
    if target == "container":
        kubectl = _kubectl()
        if kubectl is None or not namespace_exists():
            ui.err("namespace ausente — rode `up`.")
            return 1
        for dep in K8S_DEPLOYMENTS:
            _run([kubectl, "-n", NS, "rollout", "restart", f"deployment/{dep}"],
                 stdout=subprocess.DEVNULL)
        ui.ok("rollout restart disparado.")
        return 0
    ui.err("alvo indefinido — use --target local|container.")
    return 1


def cmd_status(args: dict) -> int:
    target = resolve_target(args["target"])
    ui.section("Status")
    if target == "local":
        running, detail = LocalService(ROOT).status()
        (ui.ok if running else ui.warn)(detail)
        return 0
    if target == "container":
        kubectl = _kubectl()
        if kubectl is None:
            ui.err("kubectl não encontrado.")
            return 1
        if not namespace_exists():
            ui.warn("namespace ausente — a stack não está no ar.")
            return 0
        _run([kubectl, "-n", NS, "get", "pods,deployments,services"])
        return 0
    ui.warn("nenhum alvo detectado — nada parece estar configurado ainda.")
    ui.info("Rode `deilebot setup` para configurar.")
    return 0


def cmd_logs(args: dict) -> int:
    target = resolve_target(args["target"])
    ui.section("Logs")
    if target == "local":
        if args["extra"]:
            ui.warn(f"filtro `{args['extra'][0]}` ignorado — no modo local "
                    "só existe o bot.")
        LocalService(ROOT).logs()
        return 0
    if target == "container":
        kubectl = _kubectl()
        if kubectl is None or not namespace_exists():
            ui.err("namespace ausente.")
            return 1
        alias = {"bot": "deilebot", "worker": "deile-worker",
                 "shell": "deile-shell"}
        which_pod = args["extra"][0] if args["extra"] else "all"
        if which_pod == "all":
            deps = ["deilebot", "deile-worker"]
        else:
            deps = [alias.get(which_pod, which_pod)]
        for dep in deps:
            ui.info(f"logs de {dep} (tail 80):")
            _run([kubectl, "-n", NS, "logs", f"deploy/{dep}", "--tail=80"])
        return 0
    ui.err("alvo indefinido — use --target local|container.")
    return 1


def cmd_reset(args: dict) -> int:
    target = resolve_target(args["target"])
    ui.section("Reset completo")
    if target == "local":
        svc = LocalService(ROOT)
        svc.stop()
        return 0 if svc.start() else 1
    if target == "container":
        ui.warn("o reset apaga e recria a stack no Kubernetes.")
        if not args["yes"] and not ui.confirm("Confirmar o reset?", default=False):
            ui.info("Cancelado.")
            return 1
        if args["rebuild"]:
            if cmd_build(args) != 0:
                return 1
        cmd_down({**args, "yes": True})
        return cmd_up(args)
    ui.err("alvo indefinido — use --target local|container.")
    return 1


# ===== doctor / help =========================================================

def cmd_doctor(args: dict) -> int:
    ui.section("Diagnóstico do ambiente")
    if not SETUP_ENV.is_file():
        ui.err(f"instalador de ambiente não encontrado em {SETUP_ENV}")
        return 1
    target = resolve_target(args["target"]) or "local"
    return _run([sys.executable, str(SETUP_ENV), "--check", "--mode", target])


def cmd_help(_args: dict) -> int:
    ui.header("deploy.py — orquestrador do deilebot / DEILE")
    ui.section("Ciclo de vida (modo local ou container)")
    ui.command_table([
        ("start", "Sobe / religa o bot."),
        ("stop", "Fecha o bot (mantém dados e configuração)."),
        ("restart", "Reinicia o bot."),
        ("status", "Mostra o estado atual."),
        ("logs", "Mostra os logs recentes."),
        ("reset", "Reset completo (--rebuild rebuilda a imagem)."),
    ])
    ui.section("Modo container (Kubernetes)")
    ui.command_table([
        ("build", "Builda a imagem deile-stack:local."),
        ("up", "Sobe a stack do zero (namespace, Secrets, deployments)."),
        ("down", "Teardown completo — apaga o namespace e os dados."),
        ("test", "Roda o Job one-shot do DEILE."),
        ("clone", "clone <owner/repo> — clona um repo no deile-shell."),
    ])
    ui.section("Ambiente e ajuda")
    ui.command_table([
        ("doctor", "Diagnostica os pré-requisitos da máquina."),
        ("help", "Mostra esta ajuda."),
    ])
    ui.section("Opções globais")
    ui.command_table([
        ("--target local|container", "Força o alvo (senão usa .deile/deploy.json)."),
        ("--yes", "Não pergunta nada (não-interativo)."),
        ("--rebuild", "No reset, rebuilda a imagem antes."),
        ("--no-color", "Desliga as cores."),
    ])
    ui.plain()
    return 0


_COMMANDS = {
    "help": cmd_help, "doctor": cmd_doctor,
    "build": cmd_build, "up": cmd_up, "down": cmd_down,
    "test": cmd_test, "clone": cmd_clone,
    "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "status": cmd_status, "logs": cmd_logs, "reset": cmd_reset,
}


def parse_args(argv: List[str]) -> dict:
    args = {"command": None, "target": None, "yes": False,
            "no_color": False, "rebuild": False, "extra": []}
    positionals: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--target" and i + 1 < len(argv):
            args["target"] = argv[i + 1]
            i += 2
            continue
        if a in ("--yes", "-y"):
            args["yes"] = True
        elif a == "--no-color":
            args["no_color"] = True
        elif a == "--rebuild":
            args["rebuild"] = True
        elif a in ("-h", "--help"):
            args["command"] = "help"
        else:
            positionals.append(a)
        i += 1
    if args["command"] is None:
        args["command"] = positionals[0] if positionals else "help"
    args["extra"] = positionals[1:]
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    if args["no_color"]:
        ui.set_color(False)
    handler = _COMMANDS.get(args["command"])
    if handler is None:
        ui.err(f"comando desconhecido: {args['command']}")
        ui.info("Rode `deploy.py help` para ver os comandos.")
        return 64
    try:
        return handler(args)
    except KeyboardInterrupt:
        ui.plain()
        ui.warn("interrompido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
