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
   código invoca ``kubectl exec`` precisa de NetworkPolicy de egress para:
   (a) o Service CIDR do cluster na porta 443 (10.43.0.0/16 em k3s), E
   (b) qualquer IP (0.0.0.0/0) na porta 6443 — necessário para Rancher
       Desktop onde o kube-proxy DNAT ClusterIP:443 não funciona e o
       kubectl precisa atingir o node IP direto na porta 6443.
   Heurística: pods com SA ``claude-creds-renewer`` precisam das duas regras.
3. Pods com SA ``claude-creds-renewer`` precisam ter
   ``KUBERNETES_SERVICE_HOST`` injetado via Downward API (fieldPath:
   status.hostIP) e ``KUBERNETES_SERVICE_PORT=6443``. Sem isso, kubectl
   usa o ClusterIP padrão (10.43.0.1:443) que falha em Rancher Desktop.
4. Todo manifest YAML carrega sem erro de sintaxe.

Quando este teste falhar, NÃO mude o teste para passar. Mude o manifest.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

MANIFEST_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "manifests"
K3S_SERVICE_CIDR = "10.43.0.0/16"


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
    return {
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "ServiceAccount"
    }


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


def _get_sa(doc: dict[str, Any]) -> str | None:
    """Retorna o serviceAccountName de um workload (Deployment, CronJob, etc.)."""
    kind = doc.get("kind", "")
    spec = doc.get("spec", {}) or {}
    if kind == "CronJob":
        pod_spec = (
            spec.get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
        ) or {}
    elif kind in {"Deployment", "Job", "DaemonSet", "StatefulSet"}:
        pod_spec = (spec.get("template", {}) or {}).get("spec", {}) or {}
    else:
        return None
    return pod_spec.get("serviceAccountName")


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
        (manifest, kind, sa)
        for manifest, kind, sa in refs
        if sa not in declared
    ]
    assert not missing, (
        f"ServiceAccount(s) referenciado em pods mas não declarado: {missing}.\n"
        f"SAs declaradas: {sorted(declared)}.\n"
        "Adicione o manifest da ServiceAccount no mesmo PR — sem ela o pod "
        "falha ao iniciar com 'serviceaccount not found' (403/Pending)."
    )


def _pod_env_vars(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrai lista de env vars de todos os containers de um workload."""
    kind = doc.get("kind", "")
    spec = doc.get("spec", {}) or {}
    if kind == "CronJob":
        pod_spec = (
            spec.get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
        ) or {}
    elif kind in {"Deployment", "Job", "DaemonSet", "StatefulSet"}:
        pod_spec = (spec.get("template", {}) or {}).get("spec", {}) or {}
    else:
        return []
    env_vars: list[dict[str, Any]] = []
    for container in (pod_spec.get("containers") or []):
        env_vars.extend(container.get("env") or [])
    return env_vars


def test_kubectl_exec_pods_have_apiserver_egress(docs: list[dict[str, Any]]) -> None:
    """Regra 2 — pods que fazem ``kubectl exec`` precisam de egress pro apiserver.

    Heurística: pods que assumem a SA ``claude-creds-renewer`` (definida no
    manifest 51 com permissão ``pods/exec`` no claude-worker) PRECISAM ter
    NetworkPolicy que permita:
    - TCP/443 para ``10.43.0.0/16`` (ClusterIP padrão in-cluster)
    - TCP/6443 para ``0.0.0.0/0`` (node IP direto — Rancher Desktop fallback)

    Sem a regra 443: ``kubectl exec`` falha com ``connection timed out`` (bug
    original, PR #420). Sem a regra 6443: em Rancher Desktop, kube-proxy DNAT
    não funciona de dentro de pods e ``kubectl exec`` falha com ``connection
    refused`` (bug atual antes desta PR).
    """
    refs = _service_account_refs(docs)
    pods_needing_apiserver = {
        manifest for manifest, _kind, sa in refs
        if sa == "claude-creds-renewer"
    }
    assert pods_needing_apiserver, (
        "Sentinel-check: nenhum pod usa SA 'claude-creds-renewer' — se o auto-renew "
        "foi removido propositalmente, atualize ou delete este teste."
    )

    egress_policies = _egress_policies(docs)
    port_443_covered: set[str] = set()
    port_6443_covered: set[str] = set()

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
            has_443 = any(
                p.get("port") == 443 and p.get("protocol", "TCP") == "TCP"
                for p in ports
            )
            has_6443 = any(
                p.get("port") == 6443 and p.get("protocol", "TCP") == "TCP"
                for p in ports
            )
            for to in to_blocks:
                ip_block = to.get("ipBlock") or {}
                cidr = ip_block.get("cidr", "")
                if has_443 and cidr == K3S_SERVICE_CIDR:
                    for app in ("deile-pipeline", "claude-creds-renewer"):
                        if covers(app):
                            port_443_covered.add(app)
                # Porta 6443: aceita qualquer CIDR suficientemente amplo
                # (0.0.0.0/0 cobre qualquer node IP).
                if has_6443 and cidr in ("0.0.0.0/0",):
                    for app in ("deile-pipeline", "claude-creds-renewer"):
                        if covers(app):
                            port_6443_covered.add(app)

    required = {"deile-pipeline", "claude-creds-renewer"}

    missing_443 = required - port_443_covered
    assert not missing_443, (
        f"Pods sem egress TCP/443 para Service CIDR {K3S_SERVICE_CIDR}: "
        f"{sorted(missing_443)}. Sem esta regra, `kubectl exec` falha com "
        "'connection timed out'. Adicione em 40-network-policy.yaml."
    )

    missing_6443 = required - port_6443_covered
    assert not missing_6443, (
        f"Pods sem egress TCP/6443 para 0.0.0.0/0: {sorted(missing_6443)}.\n"
        "Esta regra é necessária para Rancher Desktop onde o kube-proxy DNAT\n"
        "ClusterIP:443 falha de dentro de pods (retorna 'connection refused').\n"
        "O kubectl deve atingir o node IP direto na porta 6443.\n"
        "Adicione em 40-network-policy.yaml:\n"
        "  - to: [{ ipBlock: { cidr: 0.0.0.0/0 } }]\n"
        "    ports: [{ protocol: TCP, port: 6443 }]"
    )


def test_kubectl_exec_pods_have_kube_api_host_override(
    docs: list[dict[str, Any]],
) -> None:
    """Regra 3 — pods com SA claude-creds-renewer precisam sobrescrever o
    endpoint do apiserver via Downward API.

    Em Rancher Desktop, ``KUBERNETES_SERVICE_HOST`` aponta para o ClusterIP
    ``10.43.0.1`` que não responde corretamente de dentro de pods (kube-proxy
    DNAT quebrado). A solução: injetar ``KUBERNETES_SERVICE_HOST=status.hostIP``
    (node IP real) e ``KUBERNETES_SERVICE_PORT=6443`` para que kubectl use o
    apiserver diretamente.

    Sem estes env vars, mesmo com a NetworkPolicy de porta 6443 aberta, o
    kubectl usaria ``10.43.0.1:443`` e continuaria falhando.
    """
    renewer_docs = [
        d for d in docs
        if d.get("kind") in {"Deployment", "CronJob", "Job", "DaemonSet", "StatefulSet"}
        and _get_sa(d) == "claude-creds-renewer"
    ]
    assert renewer_docs, (
        "Sentinel: nenhum workload usa SA 'claude-creds-renewer'. "
        "Se o auto-renew foi removido, delete este teste."
    )
    missing_override: list[str] = []
    for doc in renewer_docs:
        env_vars = _pod_env_vars(doc)
        env_names = {e.get("name") for e in env_vars}
        # Verificar que KUBERNETES_SERVICE_HOST é injetado via Downward API
        has_host_downward = any(
            e.get("name") == "KUBERNETES_SERVICE_HOST"
            and (e.get("valueFrom") or {}).get("fieldRef", {}).get("fieldPath") == "status.hostIP"
            for e in env_vars
        )
        has_port_6443 = any(
            e.get("name") == "KUBERNETES_SERVICE_PORT"
            and str(e.get("value", "")) == "6443"
            for e in env_vars
        )
        if not (has_host_downward and has_port_6443):
            source = doc.get("_source_file", "?")
            missing_override.append(
                f"{source} ({doc.get('kind')}/{doc.get('metadata', {}).get('name', '?')}): "
                f"host_via_downward={has_host_downward}, port_6443={has_port_6443}"
            )
    assert not missing_override, (
        "Workloads com SA 'claude-creds-renewer' sem override do endpoint do "
        "apiserver via Downward API:\n"
        + "\n".join(f"  - {m}" for m in missing_override)
        + "\n\nAdicione ao bloco env: do container:\n"
        "  - name: KUBERNETES_SERVICE_HOST\n"
        "    valueFrom:\n"
        "      fieldRef:\n"
        "        fieldPath: status.hostIP\n"
        "  - { name: KUBERNETES_SERVICE_PORT, value: '6443' }"
    )
