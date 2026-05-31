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

from pathlib import Path
from typing import Any

import pytest
import yaml

MANIFEST_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "manifests"
# Portas válidas para alcançar o kube-apiserver: 6443 (k3s/kubeadm) e 443
# (EKS/GKE/AKS). NetworkPolicy filtra o IP/porta pós-DNAT (endpoint real), não
# o ClusterIP — por isso aceitamos qualquer ipBlock RFC1918 nessas portas.
APISERVER_PORTS = frozenset({443, 6443})


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
        manifest for manifest, _kind, sa in refs
        if sa == "claude-creds-renewer"
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
        "    policyTypes: [\"Egress\"]\n"
        "    egress:\n"
        "      - to:\n"
        "          - ipBlock: { cidr: 10.0.0.0/8 }\n"
        "          - ipBlock: { cidr: 172.16.0.0/12 }\n"
        "          - ipBlock: { cidr: 192.168.0.0/16 }\n"
        "        ports: [{ protocol: TCP, port: 443 }, { protocol: TCP, port: 6443 }]"
    )
