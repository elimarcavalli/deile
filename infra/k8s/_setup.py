"""Setup interativo da stack DEILE em Kubernetes (issue #328).

Verbo ``python3 infra/k8s/deploy.py k8s setup``. Conduz o operador, em
um único fluxo guiado, do host zero (sem kubectl, sem cluster, sem
namespace DEILE) até pipeline rodando, garantindo que nenhum valor
sensível atravesse a history do shell — todo segredo entra por
``getpass.getpass`` via :func:`_cli_ui.ask_secret` e é aplicado a Secrets
K8s por arquivo temporário ``0600`` (mesma estratégia do
``deploy._apply_secret``), nunca por argv.

A função pública é :func:`run_setup` — ``deploy.py`` injeta callbacks
para reusar helpers já existentes (``_kubectl``, ``_apply_secret``,
``cluster_reachable``, ``k8s_status``). Mantém o módulo testável em
isolamento sem precisar mockar o orquestrador inteiro.
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _cli_ui as ui  # noqa: E402

LLM_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY")

_NS_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)+$")

# Prefixos conservadores; o cru também aceita "qualquer coisa não-vazia"
# quando o operador escolhe pular validação. Não buscamos detectar 100%
# dos formatos (tokens evoluem) — apenas pegar o erro grosseiro de colar
# uma chave do provider errado.
_TOKEN_PATTERNS: Dict[str, re.Pattern] = {
    "ANTHROPIC_API_KEY": re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$"),
    "OPENAI_API_KEY": re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$"),
    "DEEPSEEK_API_KEY": re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$"),
    "GOOGLE_API_KEY": re.compile(r"^[A-Za-z0-9_\-]{20,}$"),
    "GITHUB_TOKEN": re.compile(
        r"^(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_\-]{20,}$"
    ),
    "GITLAB_TOKEN": re.compile(
        r"^(glpat-|gldt-|glptt-|glsoat-)[A-Za-z0-9_\-]{16,}$"
    ),
    "DEILE_BOT_DISCORD_TOKEN": re.compile(
        r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$"
    ),
}


@dataclass
class NamespacePlan:
    """Plano declarativo de provisionamento de um namespace DEILE.

    Tudo o que o operador escolheu em prompt vive aqui — Secrets e
    ConfigMap por NS são derivados deste objeto sem novo prompt.

    Campos sensíveis (tokens, bearers, llm_keys) usam ``field(repr=False)``
    para que ``logger.exception(plan)`` ou qualquer formatador padrão de
    dataclass não vaze segredos na trilha de auditoria.
    """

    name: str
    forge_kind: str  # "github" | "gitlab" | "auto" (auto = pipeline detecta GH vs GL por URL)
    repo: str
    dispatch_mode: str  # "deile_worker" | "claude_subprocess"
    bot_enabled: bool = False  # True quando o operador habilitou bot Discord neste NS
    llm_keys: Dict[str, str] = field(default_factory=dict, repr=False)
    github_token: str = field(default="", repr=False)
    gitlab_token: str = field(default="", repr=False)
    discord_token: str = field(default="", repr=False)
    bot_bearer: str = field(default="", repr=False)  # gerado automaticamente se vazio
    worker_bearer: str = field(default="", repr=False)  # gerado automaticamente se vazio

    def secrets_kv(self) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        """Retorna (bot_secrets, deile_secrets, worker_bearer) pra aplicar."""
        bot = {
            "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN": self.bot_bearer,
            **self.llm_keys,
        }
        if self.discord_token:
            bot["DEILE_BOT_DISCORD_TOKEN"] = self.discord_token
        deile = {"DEILE_BOT_AUTH_TOKEN": self.bot_bearer, **self.llm_keys}
        if self.github_token:
            deile["GITHUB_TOKEN"] = self.github_token
        if self.gitlab_token:
            deile["GITLAB_TOKEN"] = self.gitlab_token
        worker = {"AUTH_TOKEN": self.worker_bearer}
        return bot, deile, worker

    def runtime_configmap_data(self) -> Dict[str, str]:
        """Gera o ``data:`` do ConfigMap ``deile-runtime-config`` por NS.

        Mantém o mesmo conjunto de chaves do manifest 47, mas aplica os
        overrides (forge.kind, pipeline.repo, dispatch_mode) escolhidos
        pelo operador. As entradas para worker/shell/oneshot/bot são
        idênticas ao manifest base — não tem variação per-NS.
        """
        pipeline_settings: Dict[str, object] = {
            "pipeline": {
                "dispatch_mode": self.dispatch_mode,
                "repo": self.repo,
                "poll_interval": 60,
            },
            "approval": {"auto": True},
        }
        if self.forge_kind in ("github", "gitlab"):
            pipeline_settings["forge"] = {"kind": self.forge_kind}
        return {
            "pipeline-settings.json": json.dumps(pipeline_settings, indent=2) + "\n",
            "worker-settings.json": (
                '{\n  "model": {\n    "preferred": "deepseek:deepseek-v4-pro"\n  },\n'
                '  "approval": {\n    "auto": true\n  }\n}\n'
            ),
            "shell-settings.json": '{\n  "approval": {\n    "auto": true\n  }\n}\n',
            "oneshot-settings.json": '{\n  "approval": {\n    "auto": true\n  }\n}\n',
            "bot-settings.json": (
                '{\n  "cron": {\n    "db_path": "/home/deile/data/cron.db"\n  }\n}\n'
            ),
        }


# ============================================================================
# F1 — detecção e instalação resiliente de kubectl/cluster (cross-platform)
# ============================================================================

def _ensure_kubernetes(
    yes: bool,
    cluster_reachable_fn: Callable[[], bool],
    setup_env_path: Path,
) -> bool:
    """Garante kubectl + cluster vivo.

    Reusa ``infra/setup_environment.py`` para a instalação cross-platform —
    não duplicamos a lógica de k3s/Rancher/colima aqui. Em Windows não há
    auto-install limpa, apenas instruções.
    """
    if cluster_reachable_fn():
        ui.ok("cluster Kubernetes acessível")
        return True

    ui.warn("nenhum cluster Kubernetes acessível neste host")
    if not setup_env_path.is_file():
        ui.err(f"instalador de ambiente não encontrado em {setup_env_path}")
        ui.info(
            "Sem ele não consigo instalar k3s/Rancher Desktop automaticamente. "
            "Instale manualmente um Kubernetes leve (Rancher Desktop / k3d / k3s) "
            "e rode `deploy.py k8s setup` de novo."
        )
        return False

    if not yes and not ui.confirm(
        "Rodar o instalador `setup_environment.py --mode container` agora?",
        default=True,
    ):
        ui.info(
            "OK — instale o k8s manualmente (Rancher Desktop / k3d / k3s) "
            "e rode `deploy.py k8s setup` de novo."
        )
        return False

    cmd = [sys.executable, str(setup_env_path), "--mode", "container"]
    if yes:
        cmd.append("--yes")
    try:
        rc = subprocess.run(cmd, cwd=str(setup_env_path.parent.parent)).returncode
    except OSError as exc:
        ui.err(f"falha ao executar setup_environment.py: {exc}")
        return False
    if rc != 0:
        ui.err("setup_environment.py saiu com erro — ambiente ainda incompleto")
        return False
    if not cluster_reachable_fn():
        ui.err(
            "instalação concluída mas o cluster ainda não responde. "
            "Em Rancher Desktop / colima, espere o Kubernetes ficar "
            "verde no app e rode `deploy.py k8s setup` de novo."
        )
        return False
    ui.ok("cluster Kubernetes pronto")
    return True


# ============================================================================
# F2 — setup interativo de N namespaces (prompts seguros + secrets + cm)
# ============================================================================

def _validate_token_format(env_var: str, raw: str) -> bool:
    """Heurística leve de prefixo. Tokens vazios retornam False (não cobra
    o erro do operador — quem chama decide se o token é opcional)."""
    raw = raw.strip()
    if not raw:
        return False
    pattern = _TOKEN_PATTERNS.get(env_var)
    return bool(pattern.fullmatch(raw)) if pattern else True


def _ask_secret_validated(env_var: str, *, optional: bool, dry_run: bool = False) -> str:
    """Pede um segredo via ``getpass``, valida prefixo, oferece pular.

    - ``dry_run=True``: retorna ``"<dry-run>"`` (sentinel não-vazio) sem
      prompt — evita forçar o operador a digitar segredos em modo de
      visualização do plano.
    - Vazio: pula (retorna "") se ``optional=True``; senão repete.
    - Inválido: avisa e pede ``s`` para aceitar mesmo assim, ``n`` para
      tentar de novo. Esse opt-out cobre quando o pattern fica defasado.
    """
    if dry_run:
        # Sentinel não-vazio — o caller distingue "operador informou" de
        # "pulado" pelo truthiness; em dry-run não há valor real, mas o
        # presença da entrada importa pra modelar o plano corretamente.
        return "<dry-run>"
    prompt = f"{env_var}"
    if optional:
        prompt += " (Enter para pular)"
    prefix = ui.paint("  ? ", "cyan", "bold")
    while True:
        raw = getpass(f"{prefix}{prompt}: ").strip()
        if not raw:
            if optional:
                ui.info(f"{env_var}: pulado")
                return ""
            ui.err("valor obrigatório.")
            continue
        if _validate_token_format(env_var, raw):
            return raw
        ui.warn(f"o valor não casa com o formato esperado de {env_var}.")
        if ui.confirm("Aceitar mesmo assim?", default=False):
            return raw


def _ask_llm_keys(plan_idx: int, dry_run: bool = False) -> Dict[str, str]:
    """Pergunta as 4 LLM keys; exige ao menos UMA (bootstrap_providers).

    Loop até obter ao menos uma chave (sem recursão — evita stack growth
    em interações longas com erros).
    """
    while True:
        ui.section(f"Ambiente {plan_idx}: chaves de LLM (ao menos uma)")
        collected: Dict[str, str] = {}
        for env_var in LLM_KEYS:
            val = _ask_secret_validated(env_var, optional=True, dry_run=dry_run)
            if val:
                collected[env_var] = val
        if collected:
            return collected
        ui.err(
            "nenhuma chave de LLM configurada — DEILE não sobe sem ao menos uma "
            "de ANTHROPIC/OPENAI/DEEPSEEK/GOOGLE. Repetindo este bloco."
        )


def _ask_forge(
    plan_idx: int, dry_run: bool = False
) -> Tuple[str, str, str, str, str]:
    """Pergunta forge_kind + repo + tokens (GH e/ou GL conforme o modo).

    Retorna ``(kind_for_settings, repo, gh_token, gl_token, ui_label)``:
    o ``ui_label`` preserva o termo escolhido pelo operador (``dual``)
    para apresentação, enquanto ``kind_for_settings`` é o que vai para o
    JSON do ConfigMap (``auto`` quando o operador escolheu ``dual``,
    deixando o detector decidir GH vs GL por URL).
    """
    while True:
        ui.section(f"Ambiente {plan_idx}: forge alvo")
        forge_choice = ui.choose(
            "Qual forge este ambiente atende?",
            [
                ("github", "GitHub.com ou GHES (single-forge)"),
                ("gitlab", "GitLab.com ou self-hosted (single-forge)"),
                ("dual", "Ambos (GH + GL) na mesma stack"),
            ],
        )
        repo_help = (
            "owner/repo (GH) ou group/(sub/)*project (GL)"
            if forge_choice in ("github", "gitlab")
            else "repo padrão do pipeline (owner/repo no caso de dual)"
        )
        while True:
            repo = ui.ask(f"Repositório principal — {repo_help}").strip()
            if _REPO_RE.fullmatch(repo):
                break
            ui.err("formato inválido — use `owner/repo` ou `group/sub/project`.")

        gh_token = ""
        gl_token = ""
        if forge_choice in ("github", "dual"):
            gh_token = _ask_secret_validated(
                "GITHUB_TOKEN",
                optional=(forge_choice == "dual"),
                dry_run=dry_run,
            )
        if forge_choice in ("gitlab", "dual"):
            gl_token = _ask_secret_validated(
                "GITLAB_TOKEN",
                optional=(forge_choice == "dual"),
                dry_run=dry_run,
            )
        if forge_choice == "dual" and not gh_token and not gl_token:
            ui.err(
                "modo dual exige ao menos um dos tokens (GH ou GL). Repetindo o forge."
            )
            continue

        kind_for_settings = "auto" if forge_choice == "dual" else forge_choice
        return kind_for_settings, repo, gh_token, gl_token, forge_choice


def _ask_bot(plan_idx: int, dry_run: bool = False) -> Tuple[bool, str, str]:
    """Pergunta token do Discord + bearer do bot. Retorna
    ``(enabled, discord_token, bearer)`` — ``enabled=False`` desliga o
    deployment do bot inteiramente neste NS (skip de ``20-bot-deployment.yaml``).
    O bearer manual é opcional (auto-gerado se vazio)."""
    ui.section(f"Ambiente {plan_idx}: bot Discord (opcional)")
    if not ui.confirm("Habilitar o bot Discord neste namespace?", default=False):
        return False, "", ""
    discord = _ask_secret_validated(
        "DEILE_BOT_DISCORD_TOKEN", optional=False, dry_run=dry_run
    )
    bearer = _ask_secret_validated(
        "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN (Enter = gerar automaticamente)",
        optional=True,
        dry_run=dry_run,
    )
    return True, discord, bearer


def _build_namespace_plan(
    plan_idx: int, existing_ns: List[str], dry_run: bool = False
) -> NamespacePlan:
    """Monta um ``NamespacePlan`` interativamente."""
    ui.section(f"Ambiente {plan_idx}: identidade")
    while True:
        suggested = "deile" if plan_idx == 1 else f"deile-{plan_idx}"
        name = ui.ask(
            "Nome do namespace Kubernetes (DNS-1123)", default=suggested
        ).strip()
        if not _NS_NAME_RE.fullmatch(name):
            ui.err(
                "nome inválido — minúsculas + dígitos + hífen, "
                "começa/termina com alfanumérico, até 63 chars."
            )
            continue
        if name in existing_ns:
            ui.warn(
                f"o namespace `{name}` já existe — `setup` não sobrescreve. "
                f"Escolha outro nome ou rode `k8s up --namespace {name}` "
                "manualmente para atualizar."
            )
            continue
        break

    dispatch_mode = ui.choose(
        "Como o pipeline dispara implementações?",
        [
            ("deile_worker", "via deile-worker (DEILE-to-DEILE, default)"),
            ("claude_subprocess", "via claude -p (CCR cloud / subscription)"),
        ],
    )
    kind, repo, gh_token, gl_token, _forge_ui = _ask_forge(plan_idx, dry_run=dry_run)
    llm = _ask_llm_keys(plan_idx, dry_run=dry_run)
    bot_enabled, discord, bearer_manual = _ask_bot(plan_idx, dry_run=dry_run)

    bot_bearer = bearer_manual or _secrets.token_urlsafe(32)
    worker_bearer = _secrets.token_urlsafe(32)

    return NamespacePlan(
        name=name,
        forge_kind=kind,
        repo=repo,
        dispatch_mode=dispatch_mode,
        bot_enabled=bot_enabled,
        llm_keys=llm,
        github_token=gh_token,
        gitlab_token=gl_token,
        discord_token=discord,
        bot_bearer=bot_bearer,
        worker_bearer=worker_bearer,
    )


def _collect_namespace_plans(
    existing_ns: List[str], dry_run: bool = False
) -> List[NamespacePlan]:
    """Coleta N planos de NS via prompts.

    Retorna sempre uma lista não-vazia (1..8 entradas — o loop interno
    valida o range). Aborts vivem no caller (rejeição de ``--yes`` em
    ``run_setup``) — esta função pressupõe fluxo interativo.
    """
    while True:
        raw = ui.ask(
            "Quantos ambientes (namespaces) DEILE você quer criar?", default="1"
        )
        try:
            count = int(raw)
        except ValueError:
            ui.err("número inválido.")
            continue
        if 1 <= count <= 8:
            break
        ui.err("escolha entre 1 e 8.")

    plans: List[NamespacePlan] = []
    taken = list(existing_ns)
    for i in range(1, count + 1):
        plan = _build_namespace_plan(i, taken, dry_run=dry_run)
        plans.append(plan)
        taken.append(plan.name)
    return plans


# ============================================================================
# Apply: namespace + secrets + configmap (por NS)
# ============================================================================

def _ensure_namespace(kubectl: str, name: str, deile_ns_label: str) -> bool:
    """Cria namespace (idempotente) com labels PSS restricted + managed-by."""
    proc = subprocess.run(
        [kubectl, "get", "namespace", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        ui.info(f"criando namespace `{name}`")
        create = subprocess.run(
            [kubectl, "create", "namespace", name],
            capture_output=True,
            text=True,
        )
        if create.returncode != 0:
            ui.err(f"falha ao criar namespace `{name}`: {create.stderr.strip()}")
            return False
    label = subprocess.run(
        [
            kubectl,
            "label",
            "namespace",
            name,
            deile_ns_label,
            "pod-security.kubernetes.io/enforce=restricted",
            "pod-security.kubernetes.io/enforce-version=v1.29",
            "pod-security.kubernetes.io/audit=restricted",
            "pod-security.kubernetes.io/audit-version=v1.29",
            "pod-security.kubernetes.io/warn=restricted",
            "pod-security.kubernetes.io/warn-version=v1.29",
            "--overwrite",
        ],
        capture_output=True,
        text=True,
    )
    if label.returncode != 0:
        ui.err(f"falha ao etiquetar namespace `{name}`: {label.stderr.strip()}")
        return False
    return True


def _apply_runtime_configmap(
    kubectl: str, ns: str, plan: NamespacePlan
) -> bool:
    """Renderiza e aplica o ``deile-runtime-config`` ConfigMap por NS.

    Materializa o JSON multilinha em arquivos sob ``TemporaryDirectory`` e
    delega a renderização ao ``kubectl create configmap --from-file ...
    --dry-run=client -o yaml`` — sem JSON em argv (escape hell no shell).
    O ConfigMap contém apenas configuração funcional (forge.kind,
    pipeline.repo, dispatch_mode); zero token. ``Path.write_text`` segue
    o ``umask`` corrente, e o diretório temp é apagado ao sair do bloco.
    """
    data = plan.runtime_configmap_data()
    with tempfile.TemporaryDirectory(prefix="deile-cm-") as tmpdir:
        tmp_path = Path(tmpdir)
        file_args: List[str] = []
        for fname, content in data.items():
            f = tmp_path / fname
            f.write_text(content, encoding="utf-8")
            file_args += [f"--from-file={fname}={f}"]
        rendered = subprocess.run(
            [
                kubectl,
                "-n",
                ns,
                "create",
                "configmap",
                "deile-runtime-config",
                *file_args,
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            capture_output=True,
            text=True,
        )
        if rendered.returncode != 0:
            ui.err(
                f"falha ao renderizar ConfigMap deile-runtime-config "
                f"para `{ns}`: {rendered.stderr.strip()}"
            )
            return False
        apply = subprocess.run(
            [kubectl, "apply", "-f", "-"],
            input=rendered.stdout,
            text=True,
            capture_output=True,
        )
        if apply.returncode != 0:
            ui.err(
                f"falha ao aplicar ConfigMap deile-runtime-config "
                f"em `{ns}`: {apply.stderr.strip()}"
            )
            return False
        # Label ``app=deile`` para alinhar com o ConfigMap base do manifest
        # 47 (filtros do panel selecionam por essa label). Falha aqui não
        # bloqueia o NS — o ConfigMap funcional já foi aplicado — mas
        # avisa para o operador não procurar bug fantasma depois.
        label = subprocess.run(
            [
                kubectl,
                "-n",
                ns,
                "label",
                "configmap",
                "deile-runtime-config",
                "app=deile",
                "--overwrite",
            ],
            capture_output=True,
            text=True,
        )
        if label.returncode != 0:
            ui.warn(
                f"[{ns}] não consegui aplicar label `app=deile` no "
                f"ConfigMap (não-fatal): {label.stderr.strip()}"
            )
    return True


def _apply_namespace(
    kubectl: str,
    plan: NamespacePlan,
    apply_secret_fn: Callable[..., bool],
    manifests_dir: Path,
    deile_ns_label: str,
) -> bool:
    """Provisiona um NS inteiro: namespace + network policy + secrets + cm +
    PVCs + Deployments. Idempotente — chama os manifests existentes.

    Quando ``plan.bot_enabled`` é False, **só o PVC e o Deployment do bot**
    (19-bot-data-pvc, 20-bot-deployment) são pulados — sem ``bot-secrets``
    com discord token, o deployment crashlooparia e travaria o
    ``_validate``. O ConfigMap ``bot-config`` (manifest 15) continua sendo
    aplicado mesmo sem bot, porque é também montado por worker (45) e
    shell (35) para a allowlist ``clonable_repos`` que o ``wrapper.py``
    consome em todos os pods.
    """
    ns = plan.name
    ui.info(f"[{ns}] provisionando namespace + labels PSS restricted")
    if not _ensure_namespace(kubectl, ns, deile_ns_label):
        return False

    ui.info(f"[{ns}] aplicando network policies")
    rc = subprocess.run(
        [kubectl, "apply", "-n", ns, "-f", str(manifests_dir / "40-network-policy.yaml")]
    ).returncode
    if rc != 0:
        ui.err(f"[{ns}] falha ao aplicar network policies")
        return False

    bot_kv, deile_kv, worker_kv = plan.secrets_kv()
    ui.info(f"[{ns}] criando Secrets (nada é impresso)")
    # bot-secrets só sai quando o bot foi habilitado — caso contrário,
    # nenhum pod consumidor existe (manifests do bot são pulados abaixo).
    if plan.bot_enabled:
        if not apply_secret_fn(kubectl, "bot-secrets", bot_kv, ns=ns):
            return False
    if not apply_secret_fn(kubectl, "deile-secrets", deile_kv, ns=ns):
        return False
    if not apply_secret_fn(kubectl, "worker-bearer", worker_kv, ns=ns):
        return False

    ui.info(f"[{ns}] aplicando ConfigMap deile-runtime-config (overrides por NS)")
    if not _apply_runtime_configmap(kubectl, ns, plan):
        return False

    ui.info(f"[{ns}] aplicando ConfigMap bot-config + PVCs + Deployments")
    # bot-config (manifest 15) é compartilhado: worker (45) e shell (35)
    # montam-no para a allowlist ``clonable_repos``. Aplica SEMPRE — só o
    # PVC e o Deployment do bot ficam fora quando ``bot_enabled=False``.
    base_manifests = (
        "15-bot-config.yaml",
        "35-deile-interactive.yaml",
        "41-worker-pvc.yaml",
        "45-deile-worker-deployment.yaml",
        "46-deile-pipeline-deployment.yaml",
    )
    bot_only_manifests = ("19-bot-data-pvc.yaml", "20-bot-deployment.yaml")
    manifests_to_apply = base_manifests + bot_only_manifests if plan.bot_enabled else base_manifests
    if not plan.bot_enabled:
        ui.info(
            f"[{ns}] bot Discord desabilitado — pulando PVC + Deployment do bot "
            "(ConfigMap bot-config é compartilhado, continua sendo aplicado)"
        )
    for manifest in manifests_to_apply:
        rc = subprocess.run(
            [kubectl, "apply", "-n", ns, "-f", str(manifests_dir / manifest)]
        ).returncode
        if rc != 0:
            ui.err(f"[{ns}] falha ao aplicar {manifest}")
            return False
    return True


# ============================================================================
# F3 — pós-validação + diagnóstico
# ============================================================================

def _wait_for_pods_ready(
    kubectl: str,
    ns: str,
    deployments: Tuple[str, ...],
    timeout_s: int = 90,
) -> bool:
    """Aguarda cada deployment ficar pronto. Falhas viram logs (não-fatal).

    ``deployments`` é o conjunto que o caller espera ver Ready — para um
    NS sem bot habilitado, ``deilebot`` é omitido pelo caller (não é
    deployado neste NS).
    """
    all_ok = True
    for dep in deployments:
        exists = subprocess.run(
            [kubectl, "-n", ns, "get", "deployment", dep],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        if exists != 0:
            ui.info(f"[{ns}] deployment/{dep} não presente — pulando wait")
            continue
        rc = subprocess.run(
            [
                kubectl,
                "-n",
                ns,
                "rollout",
                "status",
                f"deployment/{dep}",
                f"--timeout={timeout_s}s",
            ]
        ).returncode
        if rc != 0:
            ui.warn(f"[{ns}] deployment/{dep} não ficou pronto em {timeout_s}s")
            subprocess.run(
                [kubectl, "-n", ns, "logs", f"deploy/{dep}", "--tail=40"],
                stderr=subprocess.STDOUT,
            )
            all_ok = False
    return all_ok


def _deployments_to_wait_for(
    plan: NamespacePlan, all_deployments: Tuple[str, ...]
) -> Tuple[str, ...]:
    """Filtra o conjunto base por ``plan.bot_enabled`` — sem bot habilitado,
    ``deilebot`` é omitido (manifest do bot foi pulado em ``_apply_namespace``).
    """
    if plan.bot_enabled:
        return all_deployments
    return tuple(d for d in all_deployments if d != "deilebot")


def _validate(
    kubectl: str,
    plans: List[NamespacePlan],
    deployments: Tuple[str, ...],
    timeout_s: int = 90,
) -> bool:
    """Para cada NS: aguarda pods esperados + imprime status final."""
    ui.section("Validação pós-setup")
    overall = True
    for plan in plans:
        ui.info(f"[{plan.name}] aguardando pods ficarem prontos…")
        ok = _wait_for_pods_ready(
            kubectl,
            plan.name,
            _deployments_to_wait_for(plan, deployments),
            timeout_s,
        )
        # Status sempre — mesmo se pods não vieram, queremos o snapshot.
        subprocess.run(
            [kubectl, "-n", plan.name, "get", "pods,deployments,services"]
        )
        if ok:
            ui.ok(f"[{plan.name}] todos os deployments prontos")
        else:
            overall = False
    return overall


# ============================================================================
# Orquestração — entry point chamado pelo deploy.py
# ============================================================================

def _print_plan(plans: List[NamespacePlan]) -> None:
    """Imprime resumo declarativo do que será aplicado (segredos como
    presença/ausência, jamais valores). O label do ``forge`` traduz
    ``auto`` para ``dual`` quando ambos os tokens estão presentes — o
    operador escolheu ``dual``, e ``auto`` é detalhe de implementação do
    ConfigMap.
    """
    ui.section("Plano consolidado")
    for plan in plans:
        ui_forge = plan.forge_kind
        if plan.forge_kind == "auto":
            ui_forge = "dual (auto-detect)"
        ui.info(
            f"  • {plan.name}: forge={ui_forge} repo={plan.repo} "
            f"dispatch={plan.dispatch_mode}"
        )
        keys_n = len(plan.llm_keys)
        flags: List[str] = [f"{keys_n} LLM key(s)"]
        if plan.github_token:
            flags.append("GITHUB_TOKEN")
        if plan.gitlab_token:
            flags.append("GITLAB_TOKEN")
        if plan.bot_enabled:
            flags.append("Discord bot")
        else:
            flags.append("bot desabilitado")
        ui.detail("    Secrets: " + ", ".join(flags))


def _summarize_partial(applied: List[NamespacePlan], failed: List[str]) -> None:
    """Emite guidance para o operador sobre cleanup/resume de NS parciais."""
    if applied:
        ui.warn(
            "Namespaces que JÁ FORAM aplicados (continuam no cluster): "
            + ", ".join(p.name for p in applied)
        )
        ui.info(
            "Para remover esses NS: rode `deploy.py --namespace <ns> k8s down` "
            "em cada um. Para tentar reaproveitar e seguir manualmente: "
            "`deploy.py --namespace <ns> k8s up`."
        )
    if failed:
        ui.err(
            "Namespaces que FALHARAM (Secrets/ConfigMap podem ter sido "
            "criados parcialmente): " + ", ".join(failed)
        )
        ui.info(
            "Verifique com `kubectl -n <ns> get all,configmap,secret` e "
            "decida entre rodar `k8s down` para limpar ou aplicar de novo "
            "via `deploy.py k8s setup`."
        )


def run_setup(
    args: dict,
    *,
    kubectl_resolver: Callable[[], Optional[str]],
    cluster_reachable_fn: Callable[[], bool],
    apply_secret_fn: Callable[..., bool],
    discover_existing_fn: Callable[[], List[str]],
    manifests_dir: Path,
    setup_env_path: Path,
    deile_ns_label: str,
    deployments: Tuple[str, ...],
) -> int:
    """Entry point. Devolve exit code (0 = sucesso, 1 = erro, 2 = abortado).

    Não usa ``announce_plan`` do deploy.py porque o plano só fica conhecido
    APÓS os prompts — imprimimos o plano internamente entre coleta e apply.
    """
    yes = bool(args.get("yes"))
    dry_run = bool(args.get("dry_run"))

    # ``--yes`` é incompatível com qualquer fase deste verbo (F1 chama
    # setup_environment.py que respeita --yes, mas F2 não pode rodar sem
    # TTY). Rejeitar logo no topo evita gastar tempo na detecção de k8s
    # para depois abortar na coleta de planos.
    if yes:
        ui.err(
            "`k8s setup` é fundamentalmente interativo (segredos via getpass) "
            "— `--yes` não se aplica. Para CI/automação não-interativa, use "
            "`deploy.py k8s up` com Secrets/ConfigMaps pré-criados via "
            "`kubectl apply` manual."
        )
        return 2

    ui.header("DEILE — setup interativo (k8s do zero ao pipeline)")
    ui.info("Este verbo:")
    ui.detail("• detecta o cluster k8s (oferece instalar se faltar)")
    ui.detail("• cria 1..N namespaces DEILE com PSS restricted")
    ui.detail("• pergunta segredos via getpass (nada vai pra history do shell)")
    ui.detail("• aplica Secrets + ConfigMap por NS")
    ui.detail("• aguarda pods ficarem prontos e imprime status")
    if dry_run:
        ui.info(
            "--dry-run: vou perguntar identidade/forge/dispatch/bot e modelar "
            "o plano, mas tokens NÃO serão pedidos e nada será aplicado."
        )

    # F1 — k8s (pulado em dry-run; ``--yes`` aqui é sempre False).
    if not dry_run:
        ui.section("1. Cluster Kubernetes")
        if not _ensure_kubernetes(False, cluster_reachable_fn, setup_env_path):
            return 1
        kubectl = kubectl_resolver()
        if kubectl is None:
            ui.err("kubectl ainda inacessível após a checagem — abortando.")
            return 1
    else:
        ui.section("1. Cluster Kubernetes (pulado em --dry-run)")
        kubectl = kubectl_resolver() or ""

    # F2 — coleta
    existing_ns = discover_existing_fn() if not dry_run else []
    if existing_ns:
        ui.info(
            "Namespaces DEILE já presentes no cluster: " + ", ".join(existing_ns)
        )
    plans = _collect_namespace_plans(existing_ns, dry_run=dry_run)
    if not plans:
        return 2

    _print_plan(plans)
    if dry_run:
        ui.warn("--dry-run: nada foi aplicado.")
        return 0
    if not ui.confirm("Confirmar e aplicar?", default=True):
        ui.info("Cancelado pelo operador.")
        return 2

    # F2 — apply
    ui.section("2. Aplicando manifests")
    applied: List[NamespacePlan] = []
    failed: List[str] = []
    for plan in plans:
        ok = _apply_namespace(
            kubectl, plan, apply_secret_fn, manifests_dir, deile_ns_label
        )
        if ok:
            applied.append(plan)
        else:
            failed.append(plan.name)
    if failed:
        ui.err(
            f"falha em {len(failed)} de {len(plans)} namespace(s): "
            + ", ".join(failed)
        )
        _summarize_partial(applied, failed)
        return 1

    # F3 — validação
    overall = _validate(kubectl, plans, deployments, timeout_s=90)

    ui.section("Resumo")
    for plan in plans:
        ui.ok(
            f"[{plan.name}] forge={plan.forge_kind} repo={plan.repo} "
            f"dispatch={plan.dispatch_mode}"
        )
    if not overall:
        ui.warn(
            "Alguns deployments não ficaram prontos no tempo esperado — veja "
            "os logs acima. Rode `deploy.py --namespace <ns> k8s logs` para "
            "diagnosticar e `k8s status` periodicamente."
        )
        return 1
    ui.ok("Setup completo. Pipeline pronto pra rodar.")
    return 0


__all__ = ["NamespacePlan", "run_setup"]
