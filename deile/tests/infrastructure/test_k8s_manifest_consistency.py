"""Lint estrutural dos manifests k8s — pega gaps de stack incompleto.

Este teste é a rede de segurança que o reviewer humano/LLM PRECISA ter
quando o diff toca ``infra/k8s/manifests/``. Foi escrito após o PR #420
(``feat/pipeline-resilience-fixes``) ter mergeado com auto-renew OAuth
implementado mas **sem** a NetworkPolicy correspondente, deixando o
``kubectl exec`` do auto-renew em ``connection timed out`` por 4 dias
até alguém perceber em produção.

Regras enforced (cada falha bloqueia merge):

1. Todo ``serviceAccountName: X`` referencia uma SA que existe.
2. Todo pod que monta um ServiceAccount custom (não-``default``) e cujo
   código invoca ``kubectl exec`` precisa de NetworkPolicy de egress
   pro kube-apiserver. NetworkPolicy filtra o IP *pós-DNAT* (endpoint real
   do apiserver), NÃO o ClusterIP — então o whitelist precisa alcançar o
   endpoint real (range RFC1918 na porta 443 ou 6443; o ``deploy.py``
   estreita para o /32 real em runtime). Heurística: pods com SA
   ``claude-creds-renewer`` OU labels ``app=deile-pipeline`` precisam dessa
   regra.
3. Todo manifest YAML carrega sem erro de sintaxe.

Quando este teste falhar, NÃO mude o teste para passar. Mude o manifest.
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MANIFEST_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "manifests"
# Portas válidas para alcançar o kube-apiserver: 6443 (k3s/kubeadm) e 443
# (EKS/GKE/AKS). NetworkPolicy filtra o IP/porta pós-DNAT (endpoint real), não
# o ClusterIP — por isso aceitamos qualquer ipBlock RFC1918 nessas portas.
APISERVER_PORTS = frozenset({443, 6443})

# Prefixos da família crítica DEILE_PIPELINE_* que o guarda de paridade cobre
# (issue #534 — "Definição operacional de 'família crítica'"). Prefixos terminam
# em "_" para casar com o stage suffix; MAX_PARALLEL é chave exata (sem wildcard).
_CRITICAL_PIPELINE_ENV_PREFIXES = frozenset(
    {
        "DEILE_PIPELINE_DISPATCH_",
        "DEILE_PIPELINE_MODEL_",
        "DEILE_PIPELINE_REASONING_",
        "DEILE_PIPELINE_TIMEOUT_S_",
        "DEILE_PIPELINE_RETRIES_",
    }
)
_CRITICAL_PIPELINE_ENV_EXACT = frozenset(
    {
        "DEILE_PIPELINE_MAX_PARALLEL",
    }
)

# Nome da egress policy do apiserver (fallback estático no manifest 40 +
# override de runtime no `deploy.py`/`_netpol.py`, mesmo nome de propósito).
_APISERVER_POLICY_NAME = "creds-renewer-egress-to-kube-api"

# Importa a FONTE ÚNICA do selector de pods do módulo de runtime, para o
# teste-guarda de paridade manifest↔runtime (sem stub: _netpol só usa stdlib).
_INFRA_K8S = MANIFEST_DIR.parent
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))
from _netpol import APISERVER_EGRESS_APPS, POLICY_NAME  # noqa: E402


def _load_all_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for path in sorted(MANIFEST_DIR.glob("*.yaml")):
        with path.open(encoding="utf-8") as fh:
            for doc in yaml.safe_load_all(fh):
                if doc and isinstance(doc, dict):
                    doc["_source_file"] = path.name
                    docs.append(doc)
    return docs


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    return _load_all_docs()


def _service_accounts(docs: list[dict[str, Any]]) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}


def _service_account_refs(docs: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Returns (manifest_file, kind, serviceAccountName) for each pod-spec
    that references a non-default SA."""
    refs: list[tuple[str, str, str]] = []
    for d in docs:
        kind = d.get("kind", "")
        if kind not in {"Deployment", "Job", "CronJob", "DaemonSet", "StatefulSet"}:
            continue
        spec = d.get("spec", {}) or {}
        pod_spec: dict[str, Any]
        if kind == "CronJob":
            pod_spec = (
                spec.get("jobTemplate", {})
                .get("spec", {})
                .get("template", {})
                .get("spec", {})
            ) or {}
        else:
            pod_spec = (spec.get("template", {}) or {}).get("spec", {}) or {}
        sa = pod_spec.get("serviceAccountName")
        if sa and sa != "default":
            refs.append((d["_source_file"], kind, sa))
    return refs


def _egress_policies(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == "NetworkPolicy"]


def test_all_manifests_parse(docs: list[dict[str, Any]]) -> None:
    """Carregamento básico — falha aqui = YAML quebrado."""
    assert docs, "Nenhum manifest YAML encontrado em infra/k8s/manifests/"


def test_service_account_refs_are_defined(docs: list[dict[str, Any]]) -> None:
    """Regra 1 — ``serviceAccountName: X`` aponta para SA que existe.

    Falha esperada antes do PR #420 mergear: nenhuma. PR #420 incluiu o
    manifest 51 com ``claude-creds-renewer`` corretamente. Este teste
    existe como guard contra futuros gaps similares.
    """
    declared = _service_accounts(docs)
    refs = _service_account_refs(docs)
    missing = [
        (manifest, kind, sa) for manifest, kind, sa in refs if sa not in declared
    ]
    assert not missing, (
        f"ServiceAccount(s) referenciado em pods mas não declarado: {missing}.\n"
        f"SAs declaradas: {sorted(declared)}.\n"
        "Adicione o manifest da ServiceAccount no mesmo PR — sem ela o pod "
        "falha ao iniciar com 'serviceaccount not found' (403/Pending)."
    )


def test_kubectl_exec_pods_have_apiserver_egress(docs: list[dict[str, Any]]) -> None:
    """Regra 2 — pods que fazem ``kubectl exec`` precisam de egress pro apiserver.

    Heurística: pods que assumem a SA ``claude-creds-renewer`` (definida no
    manifest 51 com permissão ``pods/exec`` no claude-worker) PRECISAM ter
    NetworkPolicy de egress que alcance o kube-apiserver — um ``ipBlock`` com
    CIDR não-vazio na porta 443 ou 6443.

    NOTA pós-mortem (substitui a heurística antiga que fixava o Service CIDR
    ``10.43.0.0/16``): NetworkPolicy filtra o IP de destino PÓS-DNAT (o endpoint
    real do apiserver, ex. ``192.168.64.2:6443``), não o ClusterIP. Whitelist do
    Service CIDR NUNCA casa com o pacote → ``connection refused``. Por isso o
    manifest carrega um fallback RFC1918 (443+6443) e o ``deploy.py`` estreita
    para o /32 real em runtime; este teste exige apenas a presença de uma regra
    de egress que possa alcançar o apiserver, sem pinar CIDR.
    """
    refs = _service_account_refs(docs)
    pods_needing_apiserver = {
        manifest for manifest, _kind, sa in refs if sa == "claude-creds-renewer"
    }
    assert pods_needing_apiserver, (
        "Sentinel-check: nenhum pod usa SA 'claude-creds-renewer' — se o auto-renew "
        "foi removido propositalmente, atualize ou delete este teste."
    )

    egress_policies = _egress_policies(docs)
    apiserver_egress_covered: set[str] = set()
    for policy in egress_policies:
        spec = policy.get("spec", {}) or {}
        if "Egress" not in (spec.get("policyTypes") or []):
            continue
        selector = (spec.get("podSelector", {}) or {}).get("matchLabels") or {}
        match_expr = (spec.get("podSelector", {}) or {}).get("matchExpressions") or []

        def covers(app_value: str) -> bool:
            if selector.get("app") == app_value:
                return True
            for expr in match_expr:
                if (
                    expr.get("key") == "app"
                    and expr.get("operator") == "In"
                    and app_value in (expr.get("values") or [])
                ):
                    return True
            return False

        for rule in spec.get("egress") or []:
            to_blocks = rule.get("to") or []
            ports = rule.get("ports") or []
            reaches_apiserver_port = any(
                p.get("port") in APISERVER_PORTS and p.get("protocol", "TCP") == "TCP"
                for p in ports
            )
            if not reaches_apiserver_port:
                continue
            has_ipblock = any((to.get("ipBlock") or {}).get("cidr") for to in to_blocks)
            if not has_ipblock:
                continue
            if covers("deile-pipeline"):
                apiserver_egress_covered.add("deile-pipeline")
            if covers("claude-creds-renewer"):
                apiserver_egress_covered.add("claude-creds-renewer")

    required = {"deile-pipeline", "claude-creds-renewer"}
    missing = required - apiserver_egress_covered
    assert not missing, (
        "Pods que invocam `kubectl exec` sem NetworkPolicy de egress capaz de "
        f"alcançar o kube-apiserver (ipBlock + porta 443/6443): {sorted(missing)}.\n"
        "Lembre: NetworkPolicy filtra o IP PÓS-DNAT (endpoint real do apiserver), "
        "NÃO o ClusterIP — liberar 10.43.0.0/16 não funciona ('connection "
        "refused'). Adicione/estenda uma NetworkPolicy de egress no manifest "
        "40-network-policy.yaml cobrindo esses pods. Exemplo de fallback portátil:\n"
        "  spec:\n"
        "    podSelector:\n"
        "      matchExpressions:\n"
        "        - { key: app, operator: In, values: [deile-pipeline, "
        "claude-creds-renewer] }\n"
        '    policyTypes: ["Egress"]\n'
        "    egress:\n"
        "      - to:\n"
        "          - ipBlock: { cidr: 10.0.0.0/8 }\n"
        "          - ipBlock: { cidr: 172.16.0.0/12 }\n"
        "          - ipBlock: { cidr: 192.168.0.0/16 }\n"
        "        ports: [{ protocol: TCP, port: 443 }, { protocol: TCP, port: 6443 }]"
    )


def _apiserver_policy(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for d in docs:
        if (
            d.get("kind") == "NetworkPolicy"
            and (d.get("metadata", {}) or {}).get("name") == _APISERVER_POLICY_NAME
        ):
            return d
    return None


def test_apiserver_egress_selector_matches_runtime() -> None:
    """Regra 4 — selector do fallback (manifest 40) == fonte única do runtime.

    O ``deploy.py``/``_netpol.py`` estreita a egress policy do apiserver
    SOBRESCREVENDO-A pelo mesmo nome (``creds-renewer-egress-to-kube-api``) —
    overwrite-by-name é o que de fato estreita, já que NetworkPolicy é aditiva.
    Logo o ``podSelector`` renderizado em runtime (lista
    ``_netpol.APISERVER_EGRESS_APPS``) DEVE casar exatamente com o do fallback
    estático no manifest 40. Se divergirem, um pod ganharia egress no fallback
    amplo mas o perderia no /32 (ou vice-versa) conforme qual verbo rodou por
    último — bug silencioso de postura inconsistente. Este teste é a trava.
    """
    assert POLICY_NAME == _APISERVER_POLICY_NAME, (
        f"_netpol.POLICY_NAME ({POLICY_NAME!r}) divergiu do nome esperado "
        f"({_APISERVER_POLICY_NAME!r}) — overwrite-by-name deixaria de estreitar."
    )
    policy = _apiserver_policy(_load_all_docs())
    assert policy is not None, (
        f"NetworkPolicy '{_APISERVER_POLICY_NAME}' (fallback) ausente do "
        "manifest 40 — sem ela `kubectl apply -f` cru perde o egress ao apiserver."
    )
    expr = (policy.get("spec", {}).get("podSelector", {}) or {}).get(
        "matchExpressions"
    ) or []
    manifest_apps: set[str] = set()
    for e in expr:
        if e.get("key") == "app" and e.get("operator") == "In":
            manifest_apps.update(e.get("values") or [])
    assert manifest_apps == set(APISERVER_EGRESS_APPS), (
        f"Selector do fallback no manifest 40 ({sorted(manifest_apps)}) divergiu "
        f"de _netpol.APISERVER_EGRESS_APPS ({sorted(APISERVER_EGRESS_APPS)}). "
        "Mantenha os dois em sincronia: o override de runtime usa a constante "
        "Python; o fallback estático usa o YAML. Editar um sem o outro deixa "
        "pods com egress inconsistente entre os dois caminhos."
    )


def _get_pipeline_container_env(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the env list of the first container in the deile-pipeline Deployment."""
    for d in docs:
        if d.get("kind") == "Deployment":
            labels = (
                (d.get("spec", {}) or {})
                .get("template", {})
                .get("metadata", {})
                .get("labels", {})
            ) or {}
            if labels.get("app") == "deile-pipeline":
                containers = (
                    (d.get("spec", {}) or {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                ) or []
                if containers:
                    return containers[0].get("env") or []
    return []


# Canonical values for the 5 per-stage timeout vars (issue #530).
_EXPECTED_TIMEOUTS: dict[str, str] = {
    "DEILE_PIPELINE_TIMEOUT_S_CLASSIFY": "600",
    "DEILE_PIPELINE_TIMEOUT_S_REFINE": "1800",
    "DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT": "2700",
    "DEILE_PIPELINE_TIMEOUT_S_PR_REVIEW": "3600",
    "DEILE_PIPELINE_TIMEOUT_S_FOLLOW_UPS": "2700",
}


def test_pipeline_timeout_vars_present_with_exact_values(
    docs: list[dict[str, Any]],
) -> None:
    """Issue #530 — 5 TIMEOUT_S_* vars must exist with canonical values.

    Fails if any key is absent, has the wrong value, or is duplicated.
    When a manifest re-apply drops these env vars the pipeline falls back to
    the global pipeline_claude_timeout (1800 s) and silently kills
    IMPLEMENT/PR_REVIEW/FOLLOW_UPS early.
    """
    env = _get_pipeline_container_env(docs)
    assert env, (
        "Nenhuma variável de ambiente encontrada no container 'deile-pipeline'. "
        "Verificar que o Deployment 46-deile-pipeline-deployment.yaml carrega o env:."
    )

    # (a) + (b) presence and exact value
    env_map: dict[str, str] = {
        e["name"]: e.get("value", "") for e in env if "name" in e
    }
    errors: list[str] = []
    for key, expected in _EXPECTED_TIMEOUTS.items():
        if key not in env_map:
            errors.append(f"  AUSENTE: {key} (esperado value={expected!r})")
        elif env_map[key] != expected:
            errors.append(
                f"  VALOR ERRADO: {key}={env_map[key]!r} (esperado {expected!r})"
            )
    assert not errors, (
        "Timeouts per-stage do manifest 46 divergem dos valores canônicos (issue #530):\n"
        + "\n".join(errors)
        + "\nNão altere o teste para passar — corrija o manifest."
    )

    # (c) no duplicate name in the full env list
    names = [e["name"] for e in env if "name" in e]
    seen: set[str] = set()
    dupes: list[str] = []
    for name in names:
        if name in seen:
            dupes.append(name)
        seen.add(name)
    assert not dupes, (
        f"Entradas env: duplicadas no container deile-pipeline: {dupes}. "
        "Kubernetes usa last-wins para duplicatas — remova as redundantes."
    )


def test_apiserver_egress_fallback_is_private() -> None:
    """Regra 5 — todo ipBlock do fallback estático está em range privado.

    O fallback é amplo de propósito (cobre o endpoint real de qualquer cluster),
    mas NÃO pode ser irrestrito: um ``0.0.0.0/0`` (ou qualquer range público)
    daria a esses pods egress ao mundo nas portas 443/6443 — o oposto da
    intenção. Exige que cada CIDR seja sub-rede de um range RFC1918.
    """
    private_nets = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ]
    policy = _apiserver_policy(_load_all_docs())
    assert policy is not None, "fallback policy ausente do manifest 40"
    offenders: list[str] = []
    for rule in policy.get("spec", {}).get("egress") or []:
        for to in rule.get("to") or []:
            cidr = (to.get("ipBlock") or {}).get("cidr")
            if not cidr:
                continue
            try:
                net = ipaddress.ip_network(cidr)
            except ValueError:
                offenders.append(f"{cidr} (CIDR inválido)")
                continue
            if not any(net.subnet_of(p) for p in private_nets):
                offenders.append(cidr)
    assert not offenders, (
        f"ipBlock(s) não-privado(s) no fallback do apiserver: {offenders}. "
        "O fallback deve liberar apenas ranges RFC1918 (10/8, 172.16/12, "
        "192.168/16) — nunca 0.0.0.0/0 nem espaço público."
    )


def _pipeline_container_env(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retorna a lista env: do container 'pipeline' no Deployment 'deile-pipeline'."""
    for d in docs:
        if (
            d.get("kind") == "Deployment"
            and (d.get("metadata", {}) or {}).get("name") == "deile-pipeline"
        ):
            containers = (
                (d.get("spec", {}) or {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            ) or []
            for c in containers:
                if c.get("name") == "pipeline":
                    return list(c.get("env") or [])
    return []


def _matches_critical_family(name: str) -> bool:
    return name in _CRITICAL_PIPELINE_ENV_EXACT or any(
        name.startswith(p) for p in _CRITICAL_PIPELINE_ENV_PREFIXES
    )


def test_pipeline_critical_env_no_duplicates_and_non_empty(
    docs: list[dict[str, Any]],
) -> None:
    """Regra 6 — família DEILE_PIPELINE_* no manifest 46: integridade de paridade.

    Dois invariantes (issue #534):

    (a) Nenhum ``name`` duplicado em todo o ``env:`` do container ``pipeline`` —
        duplicata silencia o valor canônico (kubectl aplica LIFO para conflitos
        de env) e é bug silencioso. Este invariante cobre TODOS os nomes,
        independentemente de prefixo.

    (b) Toda env var da família crítica que já esteja presente no ``env:`` **e**
        que use o campo ``value`` direto (não ``valueFrom``) possui valor não-vazio
        — string vazia reverte silenciosamente para a cadeia de fallback
        (settings.json → built-in) sem qualquer log.

    A asserção NÃO exige que qualquer chave da família esteja presente no
    manifest (cenário A / cenário C de #530/#534 — sem drift material, nenhuma
    chave precisa estar lá). O guarda só impede que chaves já presentes sejam
    inválidas ou duplicadas.

    Quando este teste falhar, NÃO mude o teste para passar. Mude o manifest.
    """
    env = _pipeline_container_env(docs)
    assert env, (
        "Container 'pipeline' do Deployment 'deile-pipeline' não encontrado ou "
        "sem entradas em env: — verifique o manifest 46."
    )

    # (a) Sem duplicatas em todo o env: (independe de prefixo).
    names = [e.get("name", "") for e in env]
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name in seen:
            duplicates.append(name)
        seen.add(name)
    assert not duplicates, (
        f"Nomes duplicados em env: do container 'pipeline' (manifest 46): "
        f"{sorted(set(duplicates))}.\n"
        "Duplicata silencia silenciosamente o valor canônico (kubectl usa LIFO "
        "para conflitos de env) — remova a entrada redundante."
    )

    # (b) Família crítica: value não-vazio quando presente com campo value direto.
    offenders: list[str] = []
    for entry in env:
        name = entry.get("name", "")
        if not _matches_critical_family(name):
            continue
        if "valueFrom" in entry:
            continue  # valor vem de ConfigMap/Secret/Downward API — aceitável
        value = entry.get("value")
        if value is None or str(value).strip() == "":
            offenders.append(name)

    assert not offenders, (
        f"Env vars da família crítica DEILE_PIPELINE_* com value vazio ou ausente "
        f"em manifest 46: {sorted(offenders)}.\n"
        "String vazia reverte silenciosamente para a cadeia de fallback "
        "(settings.json → built-in) sem log — declare o valor explicitamente "
        "ou remova a entrada do manifest."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Issue #515 — personalização versionada dos workers (agents ConfigMaps)
# ─────────────────────────────────────────────────────────────────────────────

_AGENTS_DIR = MANIFEST_DIR.parent / "agents"
_CONFIGMAP_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB limite rígido do Kubernetes


def _get_configmap(docs: list[dict], name: str) -> dict | None:
    for d in docs:
        if (
            d.get("kind") == "ConfigMap"
            and (d.get("metadata", {}) or {}).get("name") == name
        ):
            return d
    return None


def _get_deployment(docs: list[dict], name: str) -> dict | None:
    for d in docs:
        if (
            d.get("kind") == "Deployment"
            and (d.get("metadata", {}) or {}).get("name") == name
        ):
            return d
    return None


def _pod_spec(deployment: dict) -> dict:
    return (deployment.get("spec", {}) or {}).get("template", {}).get("spec", {}) or {}


def test_agents_configmaps_exist(docs: list[dict]) -> None:
    """Issue #515 — ConfigMaps de agents devem existir nos manifests."""
    cm_claude = _get_configmap(docs, "claude-worker-agents")
    assert cm_claude is not None, (
        "ConfigMap 'claude-worker-agents' ausente nos manifests. "
        "Adicione 48-claude-worker-agents-configmap.yaml (issue #515)."
    )
    cm_deile = _get_configmap(docs, "deile-worker-agents")
    assert cm_deile is not None, (
        "ConfigMap 'deile-worker-agents' ausente nos manifests. "
        "Adicione 48b-deile-worker-agents-configmap.yaml (issue #515)."
    )


def test_claude_worker_agents_configmap_size(docs: list[dict]) -> None:
    """Issue #515 AC#7 — conteúdo do ConfigMap claude-worker-agents < 1 MiB."""
    cm = _get_configmap(docs, "claude-worker-agents")
    assert (
        cm is not None
    ), "ConfigMap 'claude-worker-agents' ausente — rode test_agents_configmaps_exist."
    data = cm.get("data") or {}
    total = sum(len((v or "").encode("utf-8")) for v in data.values())
    assert total < _CONFIGMAP_LIMIT_BYTES, (
        f"ConfigMap 'claude-worker-agents' excede o limite de 1 MiB do Kubernetes: "
        f"{total} bytes. Para catálogos maiores, use entrega via imagem baked (issue #516)."
    )


def test_deile_worker_agents_configmap_size(docs: list[dict]) -> None:
    """Issue #515 AC#7 — conteúdo do ConfigMap deile-worker-agents < 1 MiB."""
    cm = _get_configmap(docs, "deile-worker-agents")
    assert (
        cm is not None
    ), "ConfigMap 'deile-worker-agents' ausente — rode test_agents_configmaps_exist."
    data = cm.get("data") or {}
    total = sum(len((v or "").encode("utf-8")) for v in data.values())
    assert total < _CONFIGMAP_LIMIT_BYTES, (
        f"ConfigMap 'deile-worker-agents' excede o limite de 1 MiB do Kubernetes: "
        f"{total} bytes. Para catálogos maiores, use entrega via imagem baked (issue #516)."
    )


def test_claude_worker_has_inject_agents_initcontainer(docs: list[dict]) -> None:
    """Issue #515 AC#2 — claude-worker deve ter initContainer inject-agents."""
    dep = _get_deployment(docs, "claude-worker")
    assert dep is not None, "Deployment 'claude-worker' não encontrado nos manifests."
    pod_spec = _pod_spec(dep)
    init_containers = pod_spec.get("initContainers") or []
    names = [c.get("name") for c in init_containers]
    assert "inject-agents" in names, (
        f"Deployment 'claude-worker' não tem initContainer 'inject-agents': {names}. "
        "O initContainer é obrigatório para injetar CLAUDE.md/skills no PVC (issue #515)."
    )


def test_claude_worker_inject_agents_uses_chmod_0644(docs: list[dict]) -> None:
    """Issue #515 AC#1 — initContainer inject-agents deve usar chmod 0644 nos arquivos."""
    dep = _get_deployment(docs, "claude-worker")
    assert dep is not None, "Deployment 'claude-worker' não encontrado."
    pod_spec = _pod_spec(dep)
    init_containers = pod_spec.get("initContainers") or []
    inject = next(
        (c for c in init_containers if c.get("name") == "inject-agents"), None
    )
    assert inject is not None, "initContainer 'inject-agents' não encontrado."
    args = inject.get("args") or []
    script = " ".join(args)
    assert "chmod 0644" in script, (
        "initContainer 'inject-agents' não chama 'chmod 0644'. "
        "O AC#1 exige mode 0644 nos arquivos injetados (issue #515)."
    )


def test_claude_worker_inject_agents_idempotent_by_cmp(docs: list[dict]) -> None:
    """Issue #515 AC#2/#4a — idempotência por conteúdo (cmp -s), não por timestamp."""
    dep = _get_deployment(docs, "claude-worker")
    assert dep is not None
    pod_spec = _pod_spec(dep)
    init_containers = pod_spec.get("initContainers") or []
    inject = next(
        (c for c in init_containers if c.get("name") == "inject-agents"), None
    )
    assert inject is not None, "initContainer 'inject-agents' não encontrado."
    args = inject.get("args") or []
    script = " ".join(args)
    assert "cmp -s" in script or "cmp" in script, (
        "initContainer 'inject-agents' não usa 'cmp -s' para comparação de conteúdo. "
        "AC#2/#4a exige idempotência por hash/diff de conteúdo, não por timestamp "
        "(expiresAt era lógica específica de credencial, não reutilizável aqui — issue #515)."
    )


def test_claude_worker_inject_agents_mounts_configmap(docs: list[dict]) -> None:
    """Issue #515 — initContainer inject-agents deve montar o ConfigMap claude-worker-agents."""
    dep = _get_deployment(docs, "claude-worker")
    assert dep is not None
    pod_spec = _pod_spec(dep)
    # verifica que o volume claude-agents-source usa o ConfigMap correto
    volumes = pod_spec.get("volumes") or []
    agents_vol = next(
        (v for v in volumes if v.get("name") == "claude-agents-source"), None
    )
    assert agents_vol is not None, (
        "Volume 'claude-agents-source' ausente no Deployment 'claude-worker'. "
        "Adicione o volume que mapeia ConfigMap 'claude-worker-agents' (issue #515)."
    )
    cm_name = (agents_vol.get("configMap") or {}).get("name")
    assert (
        cm_name == "claude-worker-agents"
    ), f"Volume 'claude-agents-source' não aponta para 'claude-worker-agents': {cm_name!r}."


def test_deile_worker_mounts_agents_configmap_readonly(docs: list[dict]) -> None:
    """Issue #515 AC#3/#4b — deile-worker deve montar deile-agents ro em /etc/deile/agents."""
    dep = _get_deployment(docs, "deile-worker")
    assert dep is not None, "Deployment 'deile-worker' não encontrado."
    pod_spec = _pod_spec(dep)
    containers = pod_spec.get("containers") or []
    worker = next((c for c in containers if c.get("name") == "worker"), None)
    assert worker is not None, "Container 'worker' não encontrado em deile-worker."

    mounts = worker.get("volumeMounts") or []
    agents_mount = next((m for m in mounts if m.get("name") == "deile-agents"), None)
    assert agents_mount is not None, (
        "VolumeMount 'deile-agents' ausente no container 'worker' do deile-worker. "
        "O mount é obrigatório para DEILE.md e skills (issue #515 AC#3)."
    )
    assert agents_mount.get("readOnly") is True, (
        "VolumeMount 'deile-agents' não é readOnly. "
        "AC#4b: o ConfigMap deve ser montado ro — idempotente por construção (issue #515)."
    )
    assert agents_mount.get("mountPath") == "/etc/deile/agents", (
        f"VolumeMount 'deile-agents' não está em /etc/deile/agents: "
        f"{agents_mount.get('mountPath')!r} (issue #515)."
    )


def test_deile_worker_agents_volume_uses_correct_configmap(docs: list[dict]) -> None:
    """Issue #515 — volume deile-agents deve referenciar ConfigMap deile-worker-agents."""
    dep = _get_deployment(docs, "deile-worker")
    assert dep is not None
    pod_spec = _pod_spec(dep)
    volumes = pod_spec.get("volumes") or []
    agents_vol = next((v for v in volumes if v.get("name") == "deile-agents"), None)
    assert (
        agents_vol is not None
    ), "Volume 'deile-agents' ausente no Deployment 'deile-worker' (issue #515)."
    cm_name = (agents_vol.get("configMap") or {}).get("name")
    assert (
        cm_name == "deile-worker-agents"
    ), f"Volume 'deile-agents' não aponta para 'deile-worker-agents': {cm_name!r}."


def test_agents_versioned_source_files_exist() -> None:
    """Issue #515 AC#8 — arquivos-fonte versionados devem existir em infra/k8s/agents/."""
    required = [
        _AGENTS_DIR / "claude-worker" / "CLAUDE.md",
        _AGENTS_DIR / "claude-worker" / "skills" / "brainstorm" / "SKILL.md",
        _AGENTS_DIR / "claude-worker" / "skills" / "brainstorm" / "PROVENANCE",
        _AGENTS_DIR / "claude-worker" / "commands" / "plan.md",
        _AGENTS_DIR / "deile-worker" / "DEILE.md",
        _AGENTS_DIR / "deile-worker" / "skills" / "deile-systematic-debug" / "SKILL.md",
    ]
    missing = [str(p) for p in required if not p.is_file()]
    assert not missing, (
        "Arquivos-fonte versionados ausentes em infra/k8s/agents/ (issue #515 AC#8):\n"
        + "\n".join(f"  {p}" for p in missing)
        + "\nEsses arquivos são a fonte autoritativa; o ConfigMap é derivado deles."
    )


def test_brainstorm_skill_provenance_has_commit_sha() -> None:
    """Issue #515 AC#8 — PROVENANCE da skill brainstorm deve registrar commit SHA."""
    provenance = _AGENTS_DIR / "claude-worker" / "skills" / "brainstorm" / "PROVENANCE"
    if not provenance.is_file():
        pytest.skip(
            "PROVENANCE ausente — coberto por test_agents_versioned_source_files_exist"
        )
    content = provenance.read_text(encoding="utf-8")
    # commit SHA: 40 chars hex
    import re

    sha_pattern = re.compile(r"\b[0-9a-f]{40}\b")
    assert sha_pattern.search(content), (
        f"PROVENANCE da skill brainstorm não contém commit SHA (40 hex chars): "
        f"{provenance}. AC#8 exige pin explícito a commit/tag para reprodutibilidade."
    )


def test_claude_worker_agents_configmap_keys_match_sources() -> None:
    """Issue #515 AC#8 — chaves do ConfigMap devem ter conteúdo idêntico às fontes versionadas."""
    source_map = {
        "CLAUDE.md": _AGENTS_DIR / "claude-worker" / "CLAUDE.md",
        "plan.md": _AGENTS_DIR / "claude-worker" / "commands" / "plan.md",
    }
    docs = _load_all_docs()
    cm = _get_configmap(docs, "claude-worker-agents")
    if cm is None:
        pytest.skip("ConfigMap ausente — coberto por test_agents_configmaps_exist")
    data = cm.get("data") or {}
    mismatches: list[str] = []
    for key, src_path in source_map.items():
        if not src_path.is_file():
            mismatches.append(f"{key}: fonte ausente em {src_path}")
            continue
        src_content = src_path.read_text(encoding="utf-8").strip()
        cm_content = (data.get(key) or "").strip()
        if src_content != cm_content:
            mismatches.append(
                f"{key}: divergência entre fonte ({src_path}) e ConfigMap. "
                f"Sincronize o conteúdo (issue #515 AC#8)."
            )
    assert not mismatches, "\n".join(mismatches)
