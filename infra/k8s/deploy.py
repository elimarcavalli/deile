#!/usr/bin/env python3
"""Orquestrador do ciclo de vida do deilebot / DEILE.

Substitui o antigo `run.sh`. Dois alvos, **sempre explícitos no verbo**
— nunca há adivinhação de qual será atingido:

    python3 infra/k8s/deploy.py                 # menu interativo (detecta o estado)
    python3 infra/k8s/deploy.py k8s   <ação>    # stack no Kubernetes
    python3 infra/k8s/deploy.py local <ação>    # bot como serviço no host (sem k8s)
    python3 infra/k8s/deploy.py doctor          # checa os pré-requisitos
    python3 infra/k8s/deploy.py help            # lista tudo

Cada comando que ALTERA algo imprime um **plano** antes de executar; use
`--dry-run` para só ver o plano sem rodar nada. Comandos de inspeção
(`status`, `logs`) não têm plano — eles são a própria inspeção.

`k8s` cobre a stack inteira (namespace, Secrets, Deployments, Job, shell).
`local` roda só o bot como serviço de segundo plano (systemd/launchd/pidfile).
Antes de qualquer operação de container os pré-requisitos são checados; se
faltar Kubernetes, o `setup_environment.py` é oferecido.

Compat: comandos antigos (`up`, `build`, `start`, ...) ainda funcionam, mas
avisam a forma nova. `reset` foi removido (use `k8s down`+`k8s up` ou
`local restart`).
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
_REPO_ROOT = _INFRA.parent
# Repo root primeiro: garante que ``from deile.<x>`` resolva mesmo quando o
# script é chamado direto (``python3 infra/k8s/deploy.py ...``), cenário em
# que sys.path[0] é ``infra/k8s/`` e o pacote ``deile/`` ficaria invisível.
# Sem isso o painel quebra ao tentar set_pipeline_dispatch_stage (issue #309
# fase 2 hotfix — dispatch_resolver indisponível: No module named ...).
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_INFRA))
import _cli_ui as ui  # noqa: E402
from _service import LocalService  # noqa: E402

HERE = Path(__file__).resolve().parent          # infra/k8s/
ROOT = _REPO_ROOT                                # raiz do repo deile/
MANIFESTS = HERE / "manifests"
ENV_FILE = ROOT / ".env"
DEPLOY_STATE = ROOT / ".deile" / "deploy.json"
SETUP_ENV = _INFRA / "setup_environment.py"

# Namespace padrão: lido do env DEILE_K8S_NAMESPACE; fallback "deile".
# Sobrescrito pelo flag global --namespace/-n em qualquer subcomando k8s.
NS_DEFAULT = os.environ.get("DEILE_K8S_NAMESPACE", "deile")
IMAGE = "deile-stack:local"
LLM_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY")
K8S_DEPLOYMENTS = ("deilebot", "deile-worker", "deile-shell", "deile-pipeline")

# Label aplicada a todos os namespaces gerenciados pelo DEILE para que
# `k8s list` possa enumerá-los sem ambiguidade.
_DEILE_NS_LABEL = "app.kubernetes.io/managed-by=deile"


# ===== helpers ===============================================================

def _ns(args: dict) -> str:
    """Resolve o namespace efetivo para um comando k8s.

    Prioridade: ``args["k8s_namespace"]`` (flag --namespace/-n) >
    ``NS_DEFAULT`` (env DEILE_K8S_NAMESPACE ou literal "deile").
    """
    return args.get("k8s_namespace") or NS_DEFAULT


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
    """Lê o alvo gravado pelo wizard em `.deile/deploy.json` (só informativo)."""
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


def namespace_exists(ns: Optional[str] = None) -> bool:
    """Verifica se o namespace ``ns`` existe no cluster.

    Se ``ns`` for omitido, usa ``NS_DEFAULT``.
    """
    ns = ns or NS_DEFAULT
    kubectl = _kubectl()
    if kubectl is None:
        return False
    return _run(
        [kubectl, "get", "namespace", ns],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) == 0


# ===== plano (prévia do que vai acontecer) ===================================

def announce_plan(args: dict, title: str, target_desc: str, steps: List[str]) -> bool:
    """Imprime o plano de uma ação que ALTERA estado, antes de executar.

    Devolve ``True`` se a execução deve seguir, ``False`` quando ``--dry-run``
    está ligado (o chamador deve então retornar 0 sem fazer nada). Comandos de
    inspeção (status/logs) não chamam isto — eles são a própria inspeção.
    """
    ui.section(f"Plano: {title}")
    ui.info(f"alvo: {target_desc}")
    for i, msg in enumerate(steps, 1):
        ui.step(i, len(steps), msg)
    ui.plain()
    if args.get("dry_run"):
        ui.warn("--dry-run: nada foi executado.")
        return False
    return True


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


# ===== k8s: build ============================================================

def _image_build_cmd() -> Optional[List[str]]:
    """Monta o comando de build conforme o runtime de container disponível.

    Dockerfile vive na RAIZ do repo (não em infra/k8s/) — BuildKit do
    nerdctl ignora exceções de ``.dockerignore`` (``!infra/k8s/<file>``)
    quando o Dockerfile está em subdir, e isso quebra o COPY de
    ``worker_server.py`` / ``claude_worker_server.py`` / etc.
    """
    dockerfile = str(ROOT / "Dockerfile")
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


def _rollout_restart_all(ns: str) -> None:
    """Reinicia os deployments existentes para pegarem a nova imagem."""
    kubectl = _kubectl()
    if kubectl is None:
        return
    for dep in K8S_DEPLOYMENTS:
        if _run([kubectl, "-n", ns, "get", "deployment", dep],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            ui.info(f"reiniciando deployment/{dep} para pegar a nova imagem")
            _run([kubectl, "-n", ns, "rollout", "restart", f"deployment/{dep}"],
                 stdout=subprocess.DEVNULL)


def k8s_build(args: dict) -> int:
    ns = _ns(args)
    restart = bool(args.get("restart"))
    steps = [f"build da imagem {IMAGE} (nerdctl/colima/docker)"]
    if restart:
        steps.append("rollout restart dos deployments existentes")
    else:
        steps.append("NÃO reinicia os pods (use --restart, ou `k8s restart`)")
    if not announce_plan(args, "k8s build", f"imagem {IMAGE}", steps):
        return 0
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
    if restart:
        _rollout_restart_all(ns)
    else:
        ui.info("imagem pronta; rode `k8s restart` (ou `k8s build --restart`) "
                "para os pods pegarem a nova imagem.")
    return 0


# ===== k8s: up / down ========================================================

def _apply_secret(kubectl: str, name: str, kv: Dict[str, str], ns: str = "") -> bool:
    """Cria/atualiza um Secret a partir de pares chave=valor.

    Usa um arquivo temporário modo 0600 (apagado em seguida) — os valores
    nunca aparecem em argv (`ps`) nem ficam num Secret pela metade.
    ``ns`` é o namespace destino; se omitido, usa ``NS_DEFAULT``.
    """
    ns = ns or NS_DEFAULT
    fd, tmp = tempfile.mkstemp(prefix="deile-secret-", suffix=".env")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for key, val in kv.items():
                fh.write(f"{key}={val}\n")
        rendered = _capture([
            kubectl, "-n", ns, "create", "secret", "generic", name,
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


def k8s_up(args: dict) -> int:
    ns = _ns(args)
    if not announce_plan(
        args, "k8s up", f"Kubernetes (namespace `{ns}`)",
        [
            "cria o namespace (se ausente) + etiqueta para DEILE",
            "aplica network policies",
            "cria/atualiza os Secrets (bot, deile, worker) — nada é impresso",
            "aplica ConfigMap, PVCs, Deployments e Services",
            "aguarda os pods ficarem prontos (até 180s cada)",
        ],
    ):
        return 0
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

    # Cria o namespace (idempotente) e etiqueta para descoberta por `k8s list`.
    # Para o namespace padrão "deile", aplica o manifest completo com PSS labels.
    # Para namespaces customizados, cria e etiqueta programaticamente.
    ui.info(f"garantindo namespace `{ns}` com label DEILE")
    if ns == "deile":
        _run([kubectl, "apply", "-f", str(MANIFESTS / "00-namespace.yaml")])
    else:
        _run([kubectl, "create", "namespace", ns, "--dry-run=client", "-o", "yaml",
              "|", kubectl, "apply", "-f", "-"],
             shell=True)
        # Garante a label de managed-by + PSS restricted para o namespace custom.
        _run([kubectl, "label", "namespace", ns,
              _DEILE_NS_LABEL,
              "pod-security.kubernetes.io/enforce=restricted",
              "pod-security.kubernetes.io/enforce-version=v1.29",
              "--overwrite"])

    ui.info("aplicando network policies")
    _run([kubectl, "apply", "-n", ns, "-f", str(MANIFESTS / "40-network-policy.yaml")])

    ui.info("criando os Secrets (nada é impresso)")
    bot_secret = {"DEILE_BOT_DISCORD_TOKEN": discord_token,
                  "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN": bearer, **llm}
    deile_secret = {"DEILE_BOT_AUTH_TOKEN": bearer, **llm}
    if github_token:
        deile_secret["GITHUB_TOKEN"] = github_token
    for name, kv in (("bot-secrets", bot_secret),
                     ("deile-secrets", deile_secret),
                     ("worker-bearer", {"AUTH_TOKEN": worker_token})):
        if not _apply_secret(kubectl, name, kv, ns=ns):
            return 1

    ui.info("aplicando ConfigMap, PVCs, Deployments e Services")
    # ConfigMaps PRIMEIRO — Pods montam essas chaves; aplicar antes evita
    # CreateContainerConfigError no primeiro rollout. ``47-deile-runtime-config``
    # carrega o settings.json layered (issue #111) consumido por pipeline /
    # worker / shell em ~/.deile/settings.json.
    for manifest in ("15-bot-config.yaml", "47-deile-runtime-config.yaml",
                     "19-bot-data-pvc.yaml",
                     "20-bot-deployment.yaml", "35-deile-interactive.yaml",
                     "41-worker-pvc.yaml", "45-deile-worker-deployment.yaml",
                     "46-deile-pipeline-deployment.yaml"):
        _run([kubectl, "apply", "-n", ns, "-f", str(MANIFESTS / manifest)])

    for dep in K8S_DEPLOYMENTS:
        _run([kubectl, "-n", ns, "rollout", "restart", f"deployment/{dep}"],
             stdout=subprocess.DEVNULL)

    ui.info("aguardando os pods ficarem prontos (até 180s cada)")
    for dep in K8S_DEPLOYMENTS:
        if _run([kubectl, "-n", ns, "rollout", "status",
                 f"deployment/{dep}", "--timeout=180s"]) != 0:
            ui.err(f"{dep} não ficou pronto.")
            _run([kubectl, "-n", ns, "logs", f"deploy/{dep}", "--tail=60"])
            return 1
    ui.ok("stack no ar.")
    return 0


def k8s_down(args: dict) -> int:
    ns = _ns(args)
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1
    if not announce_plan(
        args, "k8s down", f"Kubernetes (namespace `{ns}`)",
        [f"DELETA o namespace `{ns}` inteiro: pods, Secrets, PVCs e "
         "TODOS os dados (histórico, cron, sessões)"],
    ):
        return 0
    ui.warn("isto é destrutivo e NÃO tem volta.")
    if not args["yes"] and not ui.confirm("Confirmar o teardown?", default=False):
        ui.info("Cancelado.")
        return 1
    rc = _run([kubectl, "delete", "namespace", ns, "--ignore-not-found"])
    if rc == 0:
        ui.ok("namespace removido.")
    return rc


# ===== k8s: setup (interativo, multi-NS, prompts seguros) ====================

def k8s_setup(args: dict) -> int:
    """Setup interativo da stack DEILE — issue #328.

    Delega a ``infra/k8s/_setup.py``. Conduz o operador do host limpo
    (sem k8s, sem NS) até o pipeline rodando, com Secrets carregados via
    ``getpass`` e ConfigMaps gerados por NS com os overrides escolhidos.

    Para CI/automação não-interativa, prefira ``k8s up`` com Secrets
    pré-criados manualmente — ``setup`` é fundamentalmente interativo
    (segredos via getpass).
    """
    # Import tardio: ``_setup`` carrega apenas em sessões interativas, não
    # paga o custo de import quando o operador chama outro verbo.
    from _setup import run_setup  # noqa: PLC0415

    # ``discover_deile_namespaces`` vive em ``_panel_data`` (mesmo módulo
    # que ``cmd_menu`` já reusa) — evita duplicar a query de label.
    def _discover() -> List[str]:
        if _kubectl() is None:
            return []
        try:
            from _panel_data import discover_deile_namespaces  # noqa: PLC0415

            return list(discover_deile_namespaces())
        except (ImportError, OSError):
            return []

    return run_setup(
        args,
        kubectl_resolver=_kubectl,
        cluster_reachable_fn=cluster_reachable,
        apply_secret_fn=_apply_secret,
        discover_existing_fn=_discover,
        manifests_dir=MANIFESTS,
        setup_env_path=SETUP_ENV,
        deile_ns_label=_DEILE_NS_LABEL,
        deployments=K8S_DEPLOYMENTS,
    )


# ===== k8s: ciclo de vida (scale / rollout) ==================================

def k8s_start(args: dict) -> int:
    ns = _ns(args)
    kubectl = _kubectl()
    if kubectl is None or not namespace_exists(ns):
        ui.err(f"namespace `{ns}` ausente — rode `deploy.py k8s up` primeiro.")
        return 1
    if not announce_plan(
        args, "k8s start", f"Kubernetes (namespace `{ns}`)",
        ["religa os deployments (scale → 1): " + ", ".join(K8S_DEPLOYMENTS)],
    ):
        return 0
    for dep in K8S_DEPLOYMENTS:
        _run([kubectl, "-n", ns, "scale", f"deployment/{dep}", "--replicas=1"],
             stdout=subprocess.DEVNULL)
    ui.ok("deployments religados (scale → 1).")
    return 0


def k8s_stop(args: dict) -> int:
    ns = _ns(args)
    kubectl = _kubectl()
    if kubectl is None or not namespace_exists(ns):
        ui.warn("nada para parar (namespace ausente).")
        return 0
    if not announce_plan(
        args, "k8s stop", f"Kubernetes (namespace `{ns}`)",
        ["escala os deployments para 0 (os dados e os Secrets ficam intactos)"],
    ):
        return 0
    for dep in K8S_DEPLOYMENTS:
        _run([kubectl, "-n", ns, "scale", f"deployment/{dep}", "--replicas=0"],
             stdout=subprocess.DEVNULL)
    ui.ok("bot parado (scale → 0).")
    return 0


def k8s_restart(args: dict) -> int:
    ns = _ns(args)
    kubectl = _kubectl()
    if kubectl is None or not namespace_exists(ns):
        ui.err(f"namespace `{ns}` ausente — rode `deploy.py k8s up`.")
        return 1
    if not announce_plan(
        args, "k8s restart", f"Kubernetes (namespace `{ns}`)",
        ["rollout restart dos deployments: " + ", ".join(K8S_DEPLOYMENTS)],
    ):
        return 0
    for dep in K8S_DEPLOYMENTS:
        _run([kubectl, "-n", ns, "rollout", "restart", f"deployment/{dep}"],
             stdout=subprocess.DEVNULL)
    ui.ok("rollout restart disparado.")
    return 0


def k8s_status(args: dict) -> int:
    ns = _ns(args)
    ui.section(f"Status — Kubernetes (namespace `{ns}`)")
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1
    if not namespace_exists(ns):
        ui.warn(f"namespace `{ns}` ausente — a stack não está no ar.")
        ui.info("Rode `deploy.py k8s up` para subir.")
        return 0
    _run([kubectl, "-n", ns, "get", "pods,deployments,services"])
    return 0


def k8s_logs(args: dict) -> int:
    ns = _ns(args)
    ui.section(f"Logs — Kubernetes (namespace `{ns}`)")
    kubectl = _kubectl()
    if kubectl is None or not namespace_exists(ns):
        ui.err(f"namespace `{ns}` ausente.")
        return 1
    alias = {"bot": "deilebot", "worker": "deile-worker", "shell": "deile-shell"}
    which_pod = args["extra"][0] if args["extra"] else "all"
    deps = ["deilebot", "deile-worker"] if which_pod == "all" \
        else [alias.get(which_pod, which_pod)]
    for dep in deps:
        ui.info(f"logs de {dep} (tail 80):")
        _run([kubectl, "-n", ns, "logs", f"deploy/{dep}", "--tail=80"])
    return 0


# ===== k8s: test (Job one-shot) ==============================================

def k8s_panel(args: dict) -> int:
    """Sobe o painel TUI ao vivo (k8s + local + GitHub + custos).

    Suporta CLI flags via `args["extra"]` (encadeados em `deploy.py k8s
    panel <flags>`):

      --namespace <ns>          Namespace k8s (default: deile)
      --pipeline-deploy <name>  Default: deile-pipeline
      --worker-deploy <name>    Default: deile-worker
      --bot-deploy <name>       Default: deilebot
      --repo <owner/repo>       Repo GitHub (default: derivado do origin)
      --usage-db <path>         SQLite de custos (default: ~/.deile/db/usage.db)
      --logs-dir <path>         Diretório de logs locais (default: ~/.deile/logs)
      --k8s-only                Não detecta processos locais nem tail de logs
      --local-only              Não tenta kubectl (mesmo se disponível)
      --demo                    Mocks puros (não toca fontes reais)
      --memdebug                Liga tracemalloc + linha "mem: ..." no head
                                do painel (sample a cada 60s, top alocador).
                                Overhead não-trivial — só para diagnóstico
                                de memory leak em sessões longas.

    Diferente da versão anterior, **não exige cluster k8s** — se k8s
    estiver fora, o painel ainda abre em "local only" (lê
    `~/.deile/logs/` + `~/.deile/db/usage.db` + processos locais).
    """
    from _panel import run_panel  # import tardio: rich só é carregado se usado
    from _panel_data import RuntimeContext

    overrides, demo_flag, standalone = _parse_panel_flags(
        args.get("extra") or []
    )
    # Validação leve antes de bootar — operador erra `--namespace` (sem valor),
    # melhor avisar antes que o painel abra com defaults silenciosos.
    if "_error" in overrides:
        ui.err(overrides["_error"])
        return 64
    # Bug-fix: a flag GLOBAL ``--namespace``/``-n`` é capturada em
    # ``args["k8s_namespace"]`` pelo argparser top-level (multi-NS no PR #315),
    # mas o ``_parse_panel_flags`` só lê ``args["extra"]`` (flags do subcomando).
    # Sem propagar a global, ``RuntimeContext.namespace`` cai no default
    # (``_NS_DEFAULT``) e o ``run_panel`` apresenta o prompt de seleção mesmo
    # com o operador tendo declarado o NS explicitamente.
    # Flag do subcomando (``deploy.py k8s panel --namespace X``) tem precedência
    # sobre a global (``deploy.py --namespace X k8s panel``) para preservar a
    # ergonomia de override pontual.
    # Resolução de namespace:
    # 1. --namespace no subcomando (overrides["namespace"]) → respeita
    # 2. -n/--namespace global (args["k8s_namespace"]) → respeita
    # 3. Nenhum dos dois → auto-discover via discover_deile_namespaces:
    #    - 0 NS DEILE detectados → cai no default _ns(args) ("deile")
    #    - 1 NS DEILE detectado → usa diretamente (mesmo que ≠ default)
    #    - ≥2 NS DEILE detectados → prompt interativo (TTY) ou warn+default (não-TTY)
    #
    # Sem este auto-discover, `deploy.py k8s panel` (sem flags) sempre cai no
    # default "deile" — operador em cluster multi-NS (GitHub+GitLab paralelos)
    # via abrir o painel vazio achando que está vendo o NS correto.
    if "namespace" not in overrides:
        explicit = args.get("k8s_namespace")
        if explicit:
            overrides["namespace"] = explicit
        else:
            from _panel_data import discover_deile_namespaces  # noqa: PLC0415
            detected = discover_deile_namespaces()
            if len(detected) == 0:
                overrides["namespace"] = _ns(args)
            elif len(detected) == 1:
                overrides["namespace"] = detected[0]
                if detected[0] != _ns(args):
                    ui.info(f"namespace auto-detectado: {detected[0]} "
                            f"(use --namespace {_ns(args)} para forçar default)")
            else:
                # Multi-NS: prompt em TTY; fallback em pipe/script.
                if sys.stdin.isatty():
                    ui.section("Multi-namespace detectado")
                    chosen = ui.choose(
                        "Qual namespace abrir no painel?",
                        [(ns, _k8s_state_label(ns)) for ns in detected],
                    )
                    overrides["namespace"] = chosen
                else:
                    overrides["namespace"] = _ns(args)
                    ui.warn(f"Multi-NS detectado ({', '.join(detected)}) "
                            f"sem TTY — usando default {overrides['namespace']!r}. "
                            f"Force com --namespace <ns>.")
    ctx = RuntimeContext.detect(**overrides)
    # K8s não é mais obrigatório, mas avisa quando o operador pediu
    # explicitamente k8s e ele não está disponível.
    if ctx.k8s_force and not ctx.k8s_available:
        ui.err("--k8s-only setado mas cluster inacessível.")
        ui.info("Rode `deploy.py k8s up` ou remova --k8s-only.")
        return 1
    if not ctx.k8s_available and not ctx.local_available and not demo_flag:
        ui.warn("Nem k8s nem rastros locais detectados.")
        ui.info("Use --demo pra ver a UI com dados sintéticos, "
                "ou inicie DEILE/k8s primeiro.")
        return 1
    # `panel` é inspeção pura (não muta nada); sem `announce_plan`.
    return run_panel(
        context=ctx,
        force_demo=demo_flag,
        memdebug=bool(standalone.get("memdebug", False)),
    )


_PANEL_FLAG_VALUE = {
    "--namespace": "namespace",
    "--pipeline-deploy": "pipeline_deploy",
    "--worker-deploy": "worker_deploy",
    "--bot-deploy": "bot_deploy",
    "--shell-deploy": "shell_deploy",
    "--repo": "repo",
    "--usage-db": "usage_db",
    "--logs-dir": "logs_dir",
    "--sessions-dir": "sessions_dir",
    "--cluster-label": "cluster_label",
    "--image-label": "image_label",
}
_PANEL_FLAG_BOOL = {
    "--k8s-only": "k8s_force",
    "--local-only": "local_force",
}
# Flags do painel que NÃO viram override do RuntimeContext (vão direto pro
# `run_panel(...)`). Mantemos esse split pra `_parse_panel_flags` continuar
# devolvendo apenas o que `RuntimeContext.detect(**overrides)` aceita.
_PANEL_FLAG_STANDALONE = {
    "--memdebug",
}


def _parse_panel_flags(extra: List[str]) -> tuple:
    """Decodifica os flags do `panel` para um dict de overrides + flags extras.

    Devolve `(overrides, demo_bool, standalone_flags)`. ``overrides`` vai
    pro ``RuntimeContext.detect(**overrides)``. ``standalone_flags`` é
    um dict de flags que não pertencem ao RuntimeContext (ex.: ``memdebug``)
    e são passadas direto pro ``run_panel(...)``. Devolve ``{"_error": msg}``
    em qualquer slot se algum flag vier sem valor / for desconhecido.
    """
    overrides: Dict[str, object] = {}
    standalone: Dict[str, object] = {}
    demo = False
    i = 0
    while i < len(extra):
        a = extra[i]
        if a == "--demo":
            demo = True
            i += 1
            continue
        if a in _PANEL_FLAG_STANDALONE:
            # Bool flags standalone — sempre True.
            standalone[a.lstrip("-").replace("-", "_")] = True
            i += 1
            continue
        if a in _PANEL_FLAG_BOOL:
            overrides[_PANEL_FLAG_BOOL[a]] = True
            i += 1
            continue
        if a in _PANEL_FLAG_VALUE:
            if i + 1 >= len(extra):
                return {"_error": f"flag `{a}` exige um valor"}, False, {}
            field = _PANEL_FLAG_VALUE[a]
            val: object = extra[i + 1]
            if field in ("usage_db", "logs_dir", "sessions_dir"):
                from pathlib import Path as _P
                val = _P(str(val)).expanduser()
            overrides[field] = val
            i += 2
            continue
        return {"_error": f"flag desconhecido: `{a}`"}, False, {}
    return overrides, demo, standalone


def k8s_test(args: dict) -> int:
    ns = _ns(args)
    kubectl = _kubectl()
    if kubectl is None or not cluster_reachable():
        ui.err("cluster Kubernetes inacessível.")
        return 1
    if not announce_plan(
        args, "k8s test", f"Kubernetes (namespace `{ns}`)",
        ["remove um Job deile-oneshot anterior (se houver)",
         "aplica o manifest 30-deile-job.yaml (prompt fixo)",
         "streama os logs do pod do Job"],
    ):
        return 0
    _run([kubectl, "-n", ns, "delete", "job", "deile-oneshot", "--ignore-not-found"],
         stdout=subprocess.DEVNULL)
    _run([kubectl, "apply", "-n", ns, "-f", str(MANIFESTS / "30-deile-job.yaml")])

    def _oneshot_pod() -> str:
        return (_capture([
            kubectl, "-n", ns, "get", "pods", "-l", "job-name=deile-oneshot",
            "-o", "jsonpath={.items[0].metadata.name}",
        ]) or "").strip()

    pod = _oneshot_pod()
    if not pod:
        time.sleep(3)
        pod = _oneshot_pod()
    if not pod:
        ui.err("o pod do Job não apareceu.")
        return 1
    ui.info("streamando os logs do Job (Ctrl-C para o stream, não o Job)")
    _run([kubectl, "-n", ns, "logs", "--pod-running-timeout=120s", "-f", pod])
    return 0


# ===== k8s: clone ============================================================

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
    # ``os.fdopen`` can raise (invalid mode, OOM building the wrapper)
    # before the ``with`` block takes ownership of ``fd``. Without this
    # guard, a failing fdopen would leak ``fd`` — an open FD to a
    # credentials file — for the process's lifetime. Same defensive
    # pattern already used in wrapper.py:205–213.
    try:
        wrapper = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with wrapper as fh:
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


def k8s_clone(args: dict) -> int:
    ns = _ns(args)
    if not args["extra"]:
        ui.err("uso: deploy.py k8s clone <owner/repo>")
        return 1
    repo = args["extra"][0]
    kubectl = _kubectl()
    if kubectl is None or not namespace_exists(ns):
        ui.err("namespace não encontrado — rode `deploy.py k8s up` primeiro.")
        return 1
    name = repo.rstrip("/").split("/")[-1]
    clone_url = f"https://github.com/{repo}.git"
    work_dir = f"/home/deile/work/{name}"
    if not announce_plan(
        args, "k8s clone", "Kubernetes (deile-shell)",
        ["injeta o GITHUB_TOKEN no Secret deile-secrets",
         "aguarda o kubelet sincronizar o token no pod (até 90s)",
         f"clona {clone_url} → {work_dir} (via guard ~/bin/git, fail-closed)"],
    ):
        return 0
    env = read_env()
    github_token = env.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        ui.err("GITHUB_TOKEN ausente no `.env` (token de leitura/clone).")
        return 1
    llm = {k: env[k] for k in LLM_KEYS if env.get(k, "").strip()}
    bearer = env.get("DEILE_BOT_AUTH_TOKEN", "").strip()
    if not bearer:
        ui.err("DEILE_BOT_AUTH_TOKEN ausente — rode `deploy.py k8s up` primeiro.")
        return 1

    ui.info("injetando o GITHUB_TOKEN no Secret deile-secrets")
    if not _apply_secret(kubectl, "deile-secrets",
                         {"DEILE_BOT_AUTH_TOKEN": bearer,
                          "GITHUB_TOKEN": github_token, **llm},
                         ns=ns):
        return 1

    ui.info("aguardando o kubelet sincronizar o token no pod (até 90s)")
    deadline = time.time() + 90
    synced = False
    while time.time() < deadline:
        if _run([kubectl, "-n", ns, "exec", "deploy/deile-shell", "--",
                 "test", "-f", "/run/secrets/deile/GITHUB_TOKEN"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            synced = True
            break
        time.sleep(10)
    if not synced:
        ui.err("o token não sincronizou no pod em 90s.")
        return 1

    ui.info(f"clonando {clone_url} → {work_dir}")
    rc = _run([kubectl, "-n", ns, "exec", "deploy/deile-shell", "--",
               "python3", "-c", _CLONE_SNIPPET, clone_url, work_dir])
    if rc == 0:
        ui.ok(f"repo disponível em {work_dir} (dentro do deile-shell).")
    return rc


# ===== local: bot como serviço no host =======================================

def local_start(args: dict) -> int:
    svc = LocalService(ROOT)
    if not announce_plan(
        args, "local start", f"host (serviço {svc.backend})",
        [f"instala/atualiza a unidade de serviço ({svc.backend})",
         "inicia o bot: python3 -m deilebot run --provider discord"],
    ):
        return 0
    return 0 if svc.start() else 1


def local_stop(args: dict) -> int:
    svc = LocalService(ROOT)
    if not announce_plan(
        args, "local stop", f"host (serviço {svc.backend})",
        ["para o bot e desabilita a unidade de serviço"],
    ):
        return 0
    return 0 if svc.stop() else 1


def local_restart(args: dict) -> int:
    svc = LocalService(ROOT)
    if not announce_plan(
        args, "local restart", f"host (serviço {svc.backend})",
        ["para o bot (se rodando)", "sobe o bot de novo"],
    ):
        return 0
    return 0 if svc.restart() else 1


def local_status(args: dict) -> int:
    ui.section("Status — local (host)")
    running, detail = LocalService(ROOT).status()
    (ui.ok if running else ui.warn)(detail)
    return 0


def local_logs(args: dict) -> int:
    ui.section("Logs — local (host)")
    if args["extra"]:
        ui.warn(f"filtro `{args['extra'][0]}` ignorado — no modo local só existe o bot.")
    LocalService(ROOT).logs()
    return 0


# ===== doctor / help / menu ==================================================

def cmd_doctor(args: dict) -> int:
    ui.section("Diagnóstico do ambiente")
    if not SETUP_ENV.is_file():
        ui.err(f"instalador de ambiente não encontrado em {SETUP_ENV}")
        return 1
    # `doctor` (ou `k8s doctor`) checa container; `local doctor` checa o host.
    mode = "local" if args.get("namespace") == "local" else "container"
    ui.info(f"checando pré-requisitos do modo: {mode}")
    return _run([sys.executable, str(SETUP_ENV), "--check", "--mode", mode])


def k8s_list(args: dict) -> int:
    """Enumera namespaces k8s com label `app.kubernetes.io/managed-by=deile`.

    Também detecta pods com `app=deile-pipeline` em qualquer namespace
    como fallback para clusters sem a label de managed-by.
    """
    ui.section("Namespaces DEILE no cluster")
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1
    # Busca por label canônica.
    by_label = _capture([
        kubectl, "get", "ns",
        "-l", _DEILE_NS_LABEL,
        "-o", "jsonpath={.items[*].metadata.name}",
    ]) or ""
    labeled = set(by_label.split()) if by_label.strip() else set()

    # Fallback: namespaces que têm pods com app=deile-pipeline.
    by_pod = _capture([
        kubectl, "get", "pods", "--all-namespaces",
        "-l", "app=deile-pipeline",
        "-o", "jsonpath={.items[*].metadata.namespace}",
    ]) or ""
    from_pods = set(by_pod.split()) if by_pod.strip() else set()

    all_ns = sorted(labeled | from_pods)
    if not all_ns:
        ui.warn("nenhum namespace DEILE encontrado no cluster.")
        ui.info("Rode `deploy.py k8s up` para provisionar o primeiro.")
        return 0
    for ns in all_ns:
        source = ""
        if ns in labeled and ns not in from_pods:
            source = " (label)"
        elif ns in from_pods and ns not in labeled:
            source = " (pods)"
        ui.info(f"  {ns}{source}")
    return 0


def _parse_claude_login_flags(extra: List[str]) -> Dict[str, object]:
    """Decodifica flags do verb `k8s claude-login` a partir de ``args["extra"]``.

    Reconhece:

      * ``--switch`` / ``--force-relogin`` -> ``force_relogin=True``
      * ``--no-interactive``               -> ``interactive=False``
      * ``--from-env-only``                -> ``from_env_only=True``
        (fail-fast se CLAUDE_OAUTH_ACCESS_TOKEN não estiver setado; implica
        ``interactive=False``)

    Devolve ``{"_error": msg}`` se vier flag desconhecida. ``--namespace``
    é resolvido pelo flag global ``-n``/``--namespace`` (via ``_ns(args)``)
    e não é re-parseado aqui.
    """
    parsed: Dict[str, object] = {
        "force_relogin": False,
        "interactive": True,
        "from_env_only": False,
    }
    for token in extra:
        if token in ("--switch", "--force-relogin"):
            parsed["force_relogin"] = True
            continue
        if token == "--no-interactive":
            parsed["interactive"] = False
            continue
        if token == "--from-env-only":
            parsed["from_env_only"] = True
            parsed["interactive"] = False  # implica non-interactive
            continue
        return {"_error": f"flag desconhecido: `{token}`"}
    return parsed


def k8s_claude_login(args: dict) -> int:
    """k8s claude-login — captura credentials Claude do host + instala claude-worker.

    Idempotente: rerodar sem flags é noop quando tudo está pronto. Use
    ``--switch`` (alias ``--force-relogin``) para forçar logout + nova OAuth
    (trocar conta). Use ``--no-interactive`` em CI para falhar se as
    credentials não estiverem presentes (não chama ``claude login``).
    Use ``--from-env-only`` para falhar-rápido se ``CLAUDE_OAUTH_ACCESS_TOKEN``
    não estiver setado (implica ``--no-interactive``; zero-touch para CI/CD).

    Issue #309 fase 2/3 — delega o trabalho pesado a
    ``infra/k8s/_claude_install.bootstrap_claude_worker``.
    """
    flags = _parse_claude_login_flags(args.get("extra") or [])
    if "_error" in flags:
        ui.err(str(flags["_error"]))
        ui.info(
            "flags válidos: --switch (--force-relogin), --no-interactive, --from-env-only"
        )
        return 64

    ns = _ns(args)

    # Import tardio: ``_claude_install`` só é necessário neste verb e em
    # operações do painel (DispatchMatrixView). Outros verbs não pagam
    # o custo do import.
    sys.path.insert(0, str(HERE))
    try:
        from _claude_install import bootstrap_claude_worker  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    force_relogin = bool(flags["force_relogin"])
    interactive = bool(flags["interactive"])
    from_env_only = bool(flags["from_env_only"])

    # Fail-fast: --from-env-only exige que a env var esteja presente.
    if from_env_only:
        import os  # noqa: PLC0415
        if not (os.environ.get("CLAUDE_OAUTH_ACCESS_TOKEN") or "").strip():
            ui.err(
                "--from-env-only: CLAUDE_OAUTH_ACCESS_TOKEN não está setada "
                "ou está vazia. Exporte a var antes de rodar."
            )
            return 1

    ui.section("k8s claude-login")
    ui.info(
        f"namespace={ns}, switch={force_relogin}, interactive={interactive}"
        + (", from_env_only=True" if from_env_only else "")
    )

    result = bootstrap_claude_worker(
        namespace=ns,
        force_relogin=force_relogin,
        interactive=interactive,
    )

    if not result.ok:
        ui.err(f"claude-login falhou: {result.error}")
        if result.account_email:
            ui.info(f"logado como: {result.account_email}")
        ui.info(
            f"Secret claude-credentials: {'ok' if result.secret_applied else '—'}"
        )
        ui.info(
            f"Deployment claude-worker:  {'ok' if result.deployment_applied else '—'}"
        )
        ui.info(
            f"Rollout ready:             {'ok' if result.rollout_ready else '—'}"
        )
        return 1

    ui.ok("claude-worker pronto.")
    if result.account_email:
        ui.info(f"logado como: {result.account_email}")
    ui.info(f"Secret claude-credentials: {'ok' if result.secret_applied else '—'}")
    ui.info(f"Deployment claude-worker:  {'ok' if result.deployment_applied else '—'}")
    ui.info(f"Rollout ready:             {'ok' if result.rollout_ready else '—'}")
    return 0


def k8s_claude_renew(args: dict) -> int:
    """k8s claude-renew — refresh lightweight do token OAuth do claude-worker.

    Use quando o claude-worker reportar ``WORKER_AUTH_EXPIRED`` ou quando
    quiser renovar PROATIVAMENTE antes da expiração (~8h do OAuth Claude).

    Diferenças vs ``claude-login`` (issue #309 fase 3 — resiliência):
      - **NÃO** abre browser (assume credentials já presentes no host).
      - **NÃO** re-aplica manifests (Deployment/PVC/ConfigMap intactos).
      - Só: lê credentials → apply Secret → rollout restart claude-worker.
      - Latência: ~30-90s (vs 2-3min do bootstrap completo).

    Útil pra:
      - Operador rodando manualmente quando vir 401 no log
      - Cron local (launchd a cada 4h) — zero-touch periódico
      - Pipeline reativo ao detectar ``WORKER_AUTH_EXPIRED`` em dispatch
    """
    ns = _ns(args)
    # Validação leve de extras (rejeitar flags desconhecidas).
    extras = args.get("extra") or []
    if extras:
        ui.warn(f"k8s claude-renew não aceita flags extras: {extras} — ignoradas")

    sys.path.insert(0, str(HERE))
    try:
        from _claude_install import renew_claude_worker  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    ui.section("k8s claude-renew")
    ui.info(f"namespace={ns} (lightweight refresh — sem manifests)")

    result = renew_claude_worker(namespace=ns)

    if not result.ok:
        ui.err(f"claude-renew falhou: {result.error}")
        if result.account_email:
            ui.info(f"conta corrente: {result.account_email}")
        ui.info(f"Secret claude-credentials: {'ok' if result.secret_applied else '—'}")
        ui.info(f"Rollout ready:             {'ok' if result.rollout_ready else '—'}")
        ui.info(
            "tente `deploy.py k8s claude-login` (full bootstrap) se este "
            "renew falhar repetidamente"
        )
        return 1

    ui.ok("claude-worker renovado.")
    if result.account_email:
        ui.info(f"logado como: {result.account_email}")
    ui.info("token novo carregado pelo pod no startup")
    return 0


def _k8s_state_label(ns: Optional[str] = None) -> str:
    """Rótulo curto do estado do k8s para o menu/diagnóstico."""
    ns = ns or NS_DEFAULT
    if _kubectl() is None:
        return "kubectl não encontrado"
    if not cluster_reachable():
        return "cluster inacessível"
    if not namespace_exists(ns):
        return f"namespace `{ns}` ausente (não provisionado)"
    pods = _capture([_kubectl(), "-n", ns, "get", "pods", "--no-headers"]) or ""
    n = len([ln for ln in pods.splitlines() if ln.strip()])
    return f"no ar ({n} pod(s)) [ns={ns}]" if n else f"provisionado, 0 pods (parado) [ns={ns}]"


# ===== k8s: create-namespace (issue #309 fase 3) =============================

class CreateNamespaceConfig:
    """Configuração para o comando ``k8s create-namespace``.

    Todas as chaves são opcionais. Defaults sensatos. O padrão de classe
    simples garante que o CLI parsing e o menu interativo passem exatamente
    o mesmo objeto para ``do_create_namespace`` — zero duplicação de regras.

    Não usa @dataclass para manter compat com Python 3.14 quando o módulo é
    carregado via ``importlib.util.exec_module`` (módulo não em sys.modules).
    """

    def __init__(
        self,
        namespace: str = "",
        forge: str = "github",
        repo: str = "",
        github_token: str = "",
        gitlab_token: str = "",
        discord_token: str = "",
        discord_owner: str = "",
        anthropic_key: str = "",
        openai_key: str = "",
        deepseek_key: str = "",
        google_key: str = "",
        worker_replicas: int = 1,
        claude_worker_replicas: int = 0,
        enable_claude_worker: bool = False,
        dry_run: bool = False,
        auto: bool = False,
    ) -> None:
        self.namespace = namespace or NS_DEFAULT  # "" → NS_DEFAULT
        self.forge = forge
        self.repo = repo
        self.github_token = github_token
        self.gitlab_token = gitlab_token
        self.discord_token = discord_token
        self.discord_owner = discord_owner
        self.anthropic_key = anthropic_key
        self.openai_key = openai_key
        self.deepseek_key = deepseek_key
        self.google_key = google_key
        self.worker_replicas = worker_replicas
        self.claude_worker_replicas = claude_worker_replicas
        self.enable_claude_worker = enable_claude_worker
        self.dry_run = dry_run
        self.auto = auto


def do_create_namespace(cfg: CreateNamespaceConfig) -> int:
    """Cria um namespace DEILE do zero com todos os parâmetros passados via cfg.

    Equivale a rodar sequencialmente:
      1. k8s up (namespace + labels + PSS + NetworkPolicies + Secrets +
                 ConfigMaps + PVCs + Deployments + Services)
      2. k8s scale --worker <n> --claude-worker <m>  (se replicas != 1/0)
      3. k8s claude-login                             (se --enable-claude-worker)

    Separado de ``k8s_up`` para (a) aceitar parâmetros via CLI sem depender
    de um ``.env`` no disco, e (b) ser invocável tanto pelo CLI quanto pelo
    menu interativo com o mesmo objeto ``CreateNamespaceConfig``.
    """
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1

    ns = cfg.namespace

    # ---- Validação mínima de tokens ----------------------------------------
    llm = {}
    for key, val in (
        ("ANTHROPIC_API_KEY", cfg.anthropic_key),
        ("OPENAI_API_KEY",    cfg.openai_key),
        ("DEEPSEEK_API_KEY",  cfg.deepseek_key),
        ("GOOGLE_API_KEY",    cfg.google_key),
    ):
        if val.strip():
            llm[key] = val.strip()

    # Fallback: tenta ler do .env do repo (para campos não passados via CLI).
    env_file = read_env()
    if not llm:
        for k in LLM_KEYS:
            if env_file.get(k, "").strip():
                llm[k] = env_file[k].strip()
    if not llm:
        ui.err("nenhuma chave de LLM fornecida (--anthropic-key / --openai-key / "
               "--deepseek-key / --google-key) nem no .env.")
        return 1

    discord_token = cfg.discord_token.strip() or env_file.get(
        "DEILE_BOT_DISCORD_TOKEN", "").strip()
    if not discord_token:
        ui.err("--discord-token ou DEILE_BOT_DISCORD_TOKEN ausente.")
        return 1

    forge_token_env = (
        "GITHUB_TOKEN" if cfg.forge == "github" else "GITLAB_TOKEN"
    )
    forge_token = (
        cfg.github_token.strip() or cfg.gitlab_token.strip()
        or env_file.get("GITHUB_TOKEN", "").strip()
        or env_file.get("GITLAB_TOKEN", "").strip()
        or env_file.get("GL_TOKEN", "").strip()
    )

    bearer = env_file.get("DEILE_BOT_AUTH_TOKEN", "").strip() or secrets.token_urlsafe(32)
    worker_token = env_file.get("DEILE_WORKER_BEARER_TOKEN", "").strip() or secrets.token_urlsafe(32)

    steps = [
        f"cria namespace `{ns}` + labels DEILE + PSS restricted",
        "aplica NetworkPolicies",
        f"cria Secrets (bot-secrets, deile-secrets, worker-bearer) — forge={cfg.forge}",
        "aplica ConfigMaps, PVCs, Deployments e Services",
        "aguarda pods ficarem prontos (até 180s cada)",
    ]
    if cfg.worker_replicas != 1:
        steps.append(f"escala deile-worker para {cfg.worker_replicas} réplicas")
    if cfg.enable_claude_worker:
        steps.append("instala claude-worker (bootstrap_claude_worker)")
    elif cfg.claude_worker_replicas > 0:
        steps.append(f"escala claude-worker para {cfg.claude_worker_replicas} réplicas")

    # Plano (sempre imprime; --dry-run aborta antes de executar).
    args_stub = {"dry_run": cfg.dry_run, "yes": cfg.auto}
    if not announce_plan(args_stub, "k8s create-namespace", f"namespace `{ns}`", steps):
        return 0

    if not cfg.auto and not ui.confirm(
        f"Confirmar criação do namespace `{ns}`?", default=True
    ):
        ui.info("Cancelado.")
        return 0

    if not ensure_container_prereqs(cfg.auto):
        return 1

    # ---- 1. Namespace + labels + PSS ----------------------------------------
    ui.info(f"garantindo namespace `{ns}` com labels DEILE + PSS restricted")
    if ns == NS_DEFAULT:
        if _run([kubectl, "apply", "-f", str(MANIFESTS / "00-namespace.yaml")]) != 0:
            ui.err("falha ao aplicar o manifest do namespace.")
            return 1
    else:
        rendered = _capture([
            kubectl, "create", "namespace", ns,
            "--dry-run=client", "-o", "yaml",
        ])
        if rendered is None:
            ui.err(f"falha ao renderizar manifest do namespace `{ns}`.")
            return 1
        apply = subprocess.run(
            [kubectl, "apply", "-f", "-"],
            input=rendered, text=True, capture_output=True,
        )
        if apply.returncode != 0:
            ui.err(f"falha ao criar namespace: {apply.stderr.strip()}")
            return 1
        label_cmd = [
            kubectl, "label", "namespace", ns,
            _DEILE_NS_LABEL.split("=")[0] + "=" + _DEILE_NS_LABEL.split("=")[1],
            "pod-security.kubernetes.io/enforce=restricted",
            "pod-security.kubernetes.io/enforce-version=v1.29",
            "--overwrite",
        ]
        _run(label_cmd, stdout=subprocess.DEVNULL)

    # ---- 2. NetworkPolicies -------------------------------------------------
    ui.info("aplicando NetworkPolicies")
    _run([kubectl, "apply", "-n", ns, "-f",
          str(MANIFESTS / "40-network-policy.yaml")])

    # ---- 3. Secrets ---------------------------------------------------------
    ui.info("criando Secrets (nada é impresso)")
    bot_secret: Dict[str, str] = {
        "DEILE_BOT_DISCORD_TOKEN": discord_token,
        "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN": bearer,
        **llm,
    }
    deile_secret: Dict[str, str] = {
        "DEILE_BOT_AUTH_TOKEN": bearer,
        **llm,
    }
    if forge_token:
        deile_secret[forge_token_env] = forge_token

    for name, kv in (
        ("bot-secrets",    bot_secret),
        ("deile-secrets",  deile_secret),
        ("worker-bearer",  {"AUTH_TOKEN": worker_token}),
    ):
        if not _apply_secret(kubectl, name, kv, ns=ns):
            return 1

    # ---- 4. ConfigMaps, PVCs, Deployments, Services -------------------------
    ui.info("aplicando ConfigMaps, PVCs, Deployments e Services")
    manifests_order = (
        "15-bot-config.yaml", "47-deile-runtime-config.yaml",
        "19-bot-data-pvc.yaml",
        "20-bot-deployment.yaml", "35-deile-interactive.yaml",
        "41-worker-pvc.yaml", "45-deile-worker-deployment.yaml",
        "46-deile-pipeline-deployment.yaml",
    )
    for manifest in manifests_order:
        path = MANIFESTS / manifest
        if path.is_file():
            _run([kubectl, "apply", "-n", ns, "-f", str(path)])

    for dep in K8S_DEPLOYMENTS:
        _run([kubectl, "-n", ns, "rollout", "restart", f"deployment/{dep}"],
             stdout=subprocess.DEVNULL)

    # ---- 5. Aguarda pods prontos -------------------------------------------
    ui.info("aguardando pods ficarem prontos (até 180s cada)")
    for dep in K8S_DEPLOYMENTS:
        if _run([kubectl, "-n", ns, "rollout", "status",
                 f"deployment/{dep}", "--timeout=180s"]) != 0:
            ui.err(f"{dep} não ficou pronto.")
            _run([kubectl, "-n", ns, "logs", f"deploy/{dep}", "--tail=60"])
            return 1

    # ---- 6. Scale worker replicas ------------------------------------------
    if cfg.worker_replicas != 1:
        ui.info(f"escalando deile-worker para {cfg.worker_replicas} réplica(s)")
        scale_cfg = ScaleConfig(
            namespace=ns, worker_replicas=cfg.worker_replicas, dry_run=False
        )
        rc = do_scale(scale_cfg)
        if rc != 0:
            return rc

    # ---- 7. claude-worker --------------------------------------------------
    if cfg.enable_claude_worker:
        ui.info("instalando claude-worker (bootstrap_claude_worker)")
        sys.path.insert(0, str(HERE))
        try:
            from _claude_install import bootstrap_claude_worker  # noqa: PLC0415
        finally:
            sys.path.pop(0)
        result = bootstrap_claude_worker(namespace=ns, force_relogin=False,
                                          interactive=not cfg.auto)
        if not result.ok:
            ui.err(f"claude-worker falhou: {result.error}")
            return 1
        ui.ok("claude-worker instalado.")
        if cfg.claude_worker_replicas > 0 and cfg.claude_worker_replicas != 1:
            scale_cfg = ScaleConfig(
                namespace=ns, claude_worker_replicas=cfg.claude_worker_replicas,
                dry_run=False,
            )
            do_scale(scale_cfg)

    ui.ok(f"namespace `{ns}` criado e stack no ar.")
    return 0


def _parse_create_namespace_flags(extra: List[str]) -> CreateNamespaceConfig:
    """Converte a lista de flags CLI em :class:`CreateNamespaceConfig`.

    Flags não reconhecidas são silenciosamente ignoradas com aviso —
    mantém compat futura sem quebrar se uma flag for removida ou renomeada.
    """
    cfg = CreateNamespaceConfig()
    i = 0
    _flag_str = {
        "--namespace":            "namespace",
        "--forge":                "forge",
        "--repo":                 "repo",
        "--github-token":         "github_token",
        "--gitlab-token":         "gitlab_token",
        "--discord-token":        "discord_token",
        "--discord-owner":        "discord_owner",
        "--anthropic-key":        "anthropic_key",
        "--openai-key":           "openai_key",
        "--deepseek-key":         "deepseek_key",
        "--google-key":           "google_key",
    }
    _flag_int = {
        "--worker-replicas":        "worker_replicas",
        "--claude-worker-replicas": "claude_worker_replicas",
    }
    _flag_bool = {
        "--enable-claude-worker": "enable_claude_worker",
        "--auto":                 "auto",
    }
    while i < len(extra):
        tok = extra[i]
        if tok in _flag_str:
            if i + 1 < len(extra):
                setattr(cfg, _flag_str[tok], extra[i + 1])
                i += 2
            else:
                ui.warn(f"flag `{tok}` sem valor — ignorada")
                i += 1
        elif tok in _flag_int:
            if i + 1 < len(extra):
                try:
                    setattr(cfg, _flag_int[tok], int(extra[i + 1]))
                except ValueError:
                    ui.warn(f"flag `{tok}` exige inteiro — ignorada")
                i += 2
            else:
                ui.warn(f"flag `{tok}` sem valor — ignorada")
                i += 1
        elif tok in _flag_bool:
            setattr(cfg, _flag_bool[tok], True)
            i += 1
        else:
            ui.warn(f"flag desconhecida para create-namespace: `{tok}` — ignorada")
            i += 1
    return cfg


def k8s_create_namespace(args: dict) -> int:
    """CLI entrypoint para ``k8s create-namespace``.

    Converte ``args["extra"]`` em :class:`CreateNamespaceConfig` e delega a
    :func:`do_create_namespace`. O namespace global (flag ``-n``/``--namespace``
    do topo) também é considerado — a flag ``--namespace`` dentro de
    ``args["extra"]`` tem precedência.
    """
    cfg = _parse_create_namespace_flags(args.get("extra") or [])

    # Flag global --namespace/-n tem precedência se --namespace local não dado
    if cfg.namespace == NS_DEFAULT and args.get("k8s_namespace"):
        cfg.namespace = args["k8s_namespace"]

    # Propaga --dry-run e --yes globais
    cfg.dry_run = bool(args.get("dry_run"))
    cfg.auto = cfg.auto or bool(args.get("yes"))

    return do_create_namespace(cfg)


# ===== k8s: scale (issue #309 fase 3 Task 3) =================================

class ScaleConfig:
    """Configuração para o comando ``k8s scale``.

    ``worker_replicas`` e ``claude_worker_replicas`` usam ``None`` como
    sentinela "não alterar" — permite escalar só um dos dois.

    Não usa @dataclass para manter compat com Python 3.14 quando o módulo é
    carregado via ``importlib.util.exec_module`` (módulo não em sys.modules).
    """

    def __init__(
        self,
        namespace: str = "",
        worker_replicas: "Optional[int]" = None,
        claude_worker_replicas: "Optional[int]" = None,
        dry_run: bool = False,
        auto: bool = False,
    ) -> None:
        self.namespace = namespace or NS_DEFAULT  # "" → NS_DEFAULT
        self.worker_replicas = worker_replicas
        self.claude_worker_replicas = claude_worker_replicas
        self.dry_run = dry_run
        self.auto = auto


def do_scale(cfg: ScaleConfig) -> int:
    """Escala ``deile-worker`` e/ou ``claude-worker`` no namespace ``cfg.namespace``.

    Usa ``kubectl scale deployment/<name> --replicas=N``. Deployments
    ausentes geram aviso mas não param a execução (idempotente se um dos
    workers não estiver instalado).

    Separado de ``k8s_scale`` (CLI entrypoint) para ser invocável pelo menu
    interativo e por :func:`do_create_namespace` com o mesmo objeto de config.
    """
    kubectl = _kubectl()
    if kubectl is None:
        ui.err("kubectl não encontrado.")
        return 1

    ns = cfg.namespace
    targets: List[tuple] = []
    if cfg.worker_replicas is not None:
        targets.append(("deile-worker", cfg.worker_replicas))
    if cfg.claude_worker_replicas is not None:
        targets.append(("claude-worker", cfg.claude_worker_replicas))

    if not targets:
        ui.warn("nenhum alvo de escala especificado "
                "(use --worker N e/ou --claude-worker N).")
        return 0

    steps = [
        f"kubectl scale deployment/{dep} --replicas={n} -n {ns}"
        for dep, n in targets
    ]
    args_stub = {"dry_run": cfg.dry_run, "yes": cfg.auto}
    if not announce_plan(args_stub, "k8s scale", f"namespace `{ns}`", steps):
        return 0

    for dep, n in targets:
        # Verifica se o deployment existe antes de tentar escalar.
        exists = _run([kubectl, "-n", ns, "get", "deployment", dep],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        if not exists:
            ui.warn(f"deployment/{dep} não encontrado em `{ns}` — ignorado.")
            continue

        rc = _run([kubectl, "-n", ns, "scale",
                   f"deployment/{dep}", f"--replicas={n}"],
                  stdout=subprocess.DEVNULL)
        if rc == 0:
            ui.ok(f"deployment/{dep} → {n} réplica(s).")
        else:
            ui.err(f"falha ao escalar {dep}.")
            return 1

    return 0


def _parse_scale_flags(extra: List[str], args: dict) -> ScaleConfig:
    """Converte flags CLI em :class:`ScaleConfig`."""
    cfg = ScaleConfig(namespace=_ns(args))
    i = 0
    while i < len(extra):
        tok = extra[i]
        if tok == "--namespace" and i + 1 < len(extra):
            cfg.namespace = extra[i + 1]; i += 2
        elif tok in ("--worker", "-w") and i + 1 < len(extra):
            try:
                cfg.worker_replicas = int(extra[i + 1])
            except ValueError:
                ui.warn(f"--worker exige inteiro — ignorado")
            i += 2
        elif tok in ("--claude-worker", "--cw") and i + 1 < len(extra):
            try:
                cfg.claude_worker_replicas = int(extra[i + 1])
            except ValueError:
                ui.warn(f"--claude-worker exige inteiro — ignorado")
            i += 2
        else:
            ui.warn(f"flag desconhecida para scale: `{tok}` — ignorada")
            i += 1
    cfg.dry_run = bool(args.get("dry_run"))
    cfg.auto = bool(args.get("yes"))
    return cfg


def k8s_scale(args: dict) -> int:
    """CLI entrypoint para ``k8s scale``.

    Exemplos::

        deploy.py k8s scale --worker 3
        deploy.py k8s scale --worker 2 --claude-worker 1
        deploy.py -n deile-gl k8s scale --worker 1 --claude-worker 0
    """
    cfg = _parse_scale_flags(args.get("extra") or [], args)
    return do_scale(cfg)


_K8S = {
    "up": k8s_up, "down": k8s_down, "start": k8s_start, "stop": k8s_stop,
    "restart": k8s_restart, "status": k8s_status, "logs": k8s_logs,
    "build": k8s_build, "test": k8s_test, "clone": k8s_clone,
    "list": k8s_list, "panel": k8s_panel, "doctor": cmd_doctor,
    "setup": k8s_setup, "claude-login": k8s_claude_login,
    "claude-renew": k8s_claude_renew,
    "create-namespace": k8s_create_namespace,
    "scale": k8s_scale,
}
_LOCAL = {
    "start": local_start, "stop": local_stop, "restart": local_restart,
    "status": local_status, "logs": local_logs, "doctor": cmd_doctor,
}

# (ação, descrição) — usado no help E no menu interativo. `clone` e
# `create-namespace` ficam fora do menu por exigirem argumentos extras.
_K8S_ACTIONS = [
    ("panel", "painel TUI ao vivo (pods + pipeline + GitHub + custos)"),
    ("setup", "setup interativo do zero ao pipeline (multi-NS, getpass)"),
    ("create-namespace", "criar namespace do zero com todos os parâmetros via CLI"),
    ("list", "listar namespaces DEILE no cluster"),
    ("status", "ver pods, deployments e services"),
    ("scale", "escalar réplicas de workers (--worker N --claude-worker M)"),
    ("up", "provisionar / atualizar a stack (idempotente)"),
    ("build", "rebuildar a imagem (--restart religa os pods)"),
    ("restart", "rollout restart dos deployments"),
    ("start", "religar (scale → 1)"),
    ("stop", "pausar (scale → 0; mantém dados e Secrets)"),
    ("logs", "logs recentes (bot + worker)"),
    ("test", "rodar o Job one-shot deile-oneshot"),
    ("clone", "clone <owner/repo> — clona um repo no deile-shell"),
    ("claude-login",
     "instalar claude-worker no cluster (flags: --switch, --no-interactive, --from-env-only)"),
    ("claude-renew",
     "renovar OAuth do claude-worker (lightweight: Secret + restart, sem manifests)"),
    ("down", "APAGAR o namespace e TODOS os dados"),
]
_LOCAL_ACTIONS = [
    ("status", "ver se o bot está rodando"),
    ("start", "subir o bot como serviço (systemd/launchd/pidfile)"),
    ("restart", "reiniciar o bot"),
    ("stop", "parar o bot"),
    ("logs", "logs recentes do bot"),
]


def cmd_help(_args: dict) -> int:
    ui.header("deploy.py — orquestrador do deilebot / DEILE")
    ui.section("Uso")
    ui.command_table([
        ("deploy.py", "menu interativo (detecta o estado de cada alvo)"),
        ("deploy.py k8s <ação>", "opera a stack no Kubernetes"),
        ("deploy.py local <ação>", "opera o bot como serviço no host (sem k8s)"),
        ("deploy.py doctor", "checa os pré-requisitos da máquina"),
        ("deploy.py --dry-run k8s <ação>", "mostra o plano e sai, sem executar"),
    ])
    ui.section("k8s — stack no Kubernetes")
    ui.command_table(_K8S_ACTIONS)
    ui.section("local — bot como serviço no host (sem k8s)")
    ui.command_table(_LOCAL_ACTIONS)
    ui.section("Flags")
    ui.command_table([
        ("--namespace <ns> / -n <ns>",
         f"namespace k8s (default: env DEILE_K8S_NAMESPACE ou \"{NS_DEFAULT}\")"),
        ("--dry-run", "mostra o plano e sai (só nos comandos que alteram algo)"),
        ("--restart", "no `k8s build`, religa os deployments após o build"),
        ("--yes / -y", "não pergunta nada (não-interativo)"),
        ("--no-color", "desliga as cores"),
    ])
    saved = read_deploy_target()
    if saved:
        ui.section("Setup")
        nice = "k8s" if saved == "container" else "local"
        ui.info(f"seu `deilebot setup` configurou o alvo: {nice}")
    ui.plain()
    return 0


def _menu_scale(args: dict) -> int:
    """Prompt interativo para ``k8s scale`` quando chamado pelo menu.

    Coleta os valores de réplicas via :func:`_cli_ui.ask`, constrói um
    :class:`ScaleConfig` e delega a :func:`do_scale`. Reutiliza exatamente
    a mesma lógica do caminho CLI — zero duplicação de regras de escala.
    """
    ns = _ns(args)
    ui.section(f"Escalar workers — namespace `{ns}`")
    ui.info("Enter vazio = manter réplicas atuais; 0 = pausar o worker.")

    raw_worker = ui.ask("Réplicas do deile-worker", default="")
    raw_cw = ui.ask("Réplicas do claude-worker", default="")

    cfg = ScaleConfig(namespace=ns, dry_run=bool(args.get("dry_run")),
                      auto=bool(args.get("yes")))
    if raw_worker.strip():
        try:
            cfg.worker_replicas = int(raw_worker.strip())
        except ValueError:
            ui.err(f"valor inválido para deile-worker: {raw_worker!r}")
            return 1
    if raw_cw.strip():
        try:
            cfg.claude_worker_replicas = int(raw_cw.strip())
        except ValueError:
            ui.err(f"valor inválido para claude-worker: {raw_cw!r}")
            return 1

    return do_scale(cfg)


def _run_action(namespace: str, action: str, args: dict) -> int:
    table = _K8S if namespace == "k8s" else _LOCAL
    handler = table.get(action)
    if handler is None:
        ui.err(f"ação desconhecida para `{namespace}`: {action}")
        ui.info(f"Ações de `{namespace}`: " + ", ".join(
            a for a, _ in (_K8S_ACTIONS if namespace == "k8s" else _LOCAL_ACTIONS)))
        return 64
    args["namespace"] = namespace
    # Para `scale` chamado pelo menu interativo (sem extra flags), coleta
    # os parâmetros via prompts — reusa exatamente do_scale() como o CLI.
    if action == "scale" and namespace == "k8s" and not args.get("extra"):
        return _menu_scale(args)
    return handler(args)


def cmd_menu(args: dict, preset_ns: Optional[str] = None) -> int:
    """Menu interativo: detecta o estado, pergunta o alvo e a ação.

    Suporta multi-namespace (issue #297): quando há mais de um namespace
    DEILE no cluster — coexistência GH + GL paralelos — o menu lista cada
    um com seu estado e pede ao operador qual será o alvo. O NS escolhido
    é propagado por ``args["k8s_namespace"]`` em todas as ações
    subsequentes (status / logs / restart / panel / ...).
    """
    if not sys.stdin.isatty():
        ui.warn("sem terminal interativo — mostrando a ajuda.")
        return cmd_help(args)
    ui.header("deploy.py — deilebot / DEILE")

    # Detecta namespaces DEILE no cluster (label
    # ``app.kubernetes.io/managed-by=deile`` + fallback por pods).
    # Import lazy: ``_panel_data`` puxa providers Rich/etc. desnecessários
    # para callers que invocam o ``deploy.py`` apenas via subcomando.
    from _panel_data import discover_deile_namespaces  # noqa: PLC0415
    detected_ns = discover_deile_namespaces() if _kubectl() is not None else []

    # Se uma flag global ``--namespace``/``-n`` foi passada, ela tem
    # precedência sobre a detecção (operador já declarou).
    explicit_ns = args.get("k8s_namespace")

    # Estado por NS — uma chamada ``_k8s_state_label`` por namespace
    # detectado (~50 ms cada via ``kubectl get pods --no-headers``).
    if detected_ns:
        ns_labels = [(ns, _k8s_state_label(ns)) for ns in detected_ns]
    else:
        # Cluster sem nenhum NS DEILE: mostra apenas o default para
        # operador rodar ``k8s up`` (que provisiona-do-zero).
        ns_labels = [(_ns(args), _k8s_state_label(_ns(args)))]

    _, local_detail = LocalService(ROOT).status()
    ui.section("Estado atual")
    for ns, label in ns_labels:
        marker = " ←" if explicit_ns == ns else ""
        ui.info(f"k8s:   {label}{marker}")
    ui.info(f"local: {local_detail}")

    namespace = preset_ns
    if namespace is None:
        # Mostra apenas {k8s, local}; se houver múltiplos NS k8s, faz um
        # segundo prompt para escolher qual NS depois que o operador
        # confirmou "k8s".
        k8s_summary = (
            f"{len(detected_ns)} ns DEILE detectados"
            if len(detected_ns) > 1
            else (ns_labels[0][1] if ns_labels else "kubectl indisponível")
        )
        namespace = ui.choose("Qual alvo?", [
            ("k8s", k8s_summary),
            ("local", local_detail),
        ])

    # Se o alvo é k8s e há múltiplos NS detectados (e nenhum foi declarado
    # explicitamente), pergunta qual usar. Caso contrário, propaga o NS já
    # resolvido por ``_ns(args)`` (flag global / env / default).
    if namespace == "k8s":
        if explicit_ns:
            args["k8s_namespace"] = explicit_ns
        elif len(detected_ns) > 1:
            ui.section("Namespace k8s")
            chosen_ns = ui.choose(
                "Qual namespace?",
                [(ns, label) for ns, label in ns_labels],
            )
            args["k8s_namespace"] = chosen_ns
        elif len(detected_ns) == 1:
            args["k8s_namespace"] = detected_ns[0]
        # Se 0 NS detectados, ``args["k8s_namespace"]`` permanece ``None``
        # e ``_ns(args)`` cai no default. Ações como ``up`` ou ``setup``
        # ainda funcionam (criam o NS).

    actions = _K8S_ACTIONS if namespace == "k8s" else _LOCAL_ACTIONS
    # `clone` e `create-namespace` precisam de argumentos — fora do menu
    # interativo. `scale` entra no menu mas com prompts internos.
    _MENU_EXCLUDED = {"clone", "create-namespace"}
    menu_actions = [(a, d) for a, d in actions if a not in _MENU_EXCLUDED]
    ns_suffix = f" (ns={args.get('k8s_namespace') or _ns(args)})" if namespace == "k8s" else ""
    ui.section(f"Ações — {namespace}{ns_suffix}")
    action = ui.choose("Qual ação?", menu_actions)
    return _run_action(namespace, action, args)


# ===== compat com a CLI antiga ===============================================

_OLD_CONTAINER = {"up", "down", "build", "test", "clone"}   # eram sempre k8s
_OLD_LIFECYCLE = {"start", "stop", "restart", "status", "logs"}  # exigiam alvo


def _handle_legacy(args: dict, positionals: List[str]) -> int:
    cmd = positionals[0]
    args["extra"] = positionals[1:]
    if cmd in _OLD_CONTAINER:
        ui.warn(f"`deploy.py {cmd}` agora é `deploy.py k8s {cmd}` — rodando isso.")
        return _run_action("k8s", cmd, args)
    if cmd == "reset":
        ui.err("`reset` foi removido (era ambíguo). Use:")
        ui.info("  k8s:   `deploy.py k8s down` e depois `deploy.py k8s up`")
        ui.info("  local: `deploy.py local restart`")
        return 64
    if cmd in _OLD_LIFECYCLE:
        ns = {"local": "local", "container": "k8s"}.get(args.get("target") or "")
        if ns:
            ui.warn(f"`deploy.py {cmd} --target {args['target']}` agora é "
                    f"`deploy.py {ns} {cmd}` — rodando isso.")
            return _run_action(ns, cmd, args)
        ui.err(f"`{cmd}` agora exige o alvo no verbo:")
        ui.info(f"  `deploy.py k8s {cmd}`   (stack no Kubernetes)")
        ui.info(f"  `deploy.py local {cmd}` (bot como serviço no host)")
        return 64
    ui.err(f"comando desconhecido: {cmd}")
    ui.info("Rode `deploy.py help` para ver os comandos.")
    return 64


# ===== parsing / main ========================================================

def parse_args(argv: List[str]) -> dict:
    # ``k8s_namespace`` é o namespace efetivo para operações k8s.
    # Lido de --namespace/-n; se omitido, ``_ns()`` cai para NS_DEFAULT.
    args = {"target": None, "yes": False, "no_color": False,
            "dry_run": False, "restart": False, "extra": [],
            "namespace": None, "k8s_namespace": None, "positionals": []}
    positionals: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--target" and i + 1 < len(argv):
            args["target"] = argv[i + 1]
            i += 2
            continue
        if a in ("--namespace", "-n") and i + 1 < len(argv):
            # Flag global de namespace k8s — qualquer subcomando k8s a herda.
            args["k8s_namespace"] = argv[i + 1]
            i += 2
            continue
        if a in ("--yes", "-y"):
            args["yes"] = True
        elif a == "--no-color":
            args["no_color"] = True
        elif a == "--dry-run":
            args["dry_run"] = True
        elif a == "--restart":
            args["restart"] = True
        elif a == "--no-restart":
            args["restart"] = False
        elif a in ("-h", "--help"):
            positionals.append("help")
        else:
            positionals.append(a)
        i += 1
    args["positionals"] = positionals
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    if args["no_color"]:
        ui.set_color(False)
    pos = args["positionals"]
    try:
        if not pos:
            return cmd_menu(args)
        head = pos[0]
        if head == "help":
            return cmd_help(args)
        if head == "doctor":
            return cmd_doctor(args)
        if head in ("k8s", "local"):
            if len(pos) < 2:
                # `deploy.py k8s` sem ação → menu já filtrado nesse alvo.
                return cmd_menu(args, preset_ns=head)
            args["extra"] = pos[2:]
            return _run_action(head, pos[1], args)
        return _handle_legacy(args, pos)
    except KeyboardInterrupt:
        ui.plain()
        ui.warn("interrompido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
