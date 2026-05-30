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
   pro Service CIDR do cluster (10.43.0.0/16 em k3s). Heurística:
   pods com SA ``claude-creds-renewer`` OU labels ``app=deile-pipeline``
   precisam dessa regra.
3. Todo manifest YAML carrega sem erro de sintaxe.

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
    """Regra 2 — pods que fazem ``kubectl exec`` precisam de egress pro Service CIDR.

    Heurística: pods que assumem a SA ``claude-creds-renewer`` (definida no
    manifest 51 com permissão ``pods/exec`` no claude-worker) PRECISAM ter
    NetworkPolicy que permita TCP/443 para ``10.43.0.0/16``. Sem isso, o
    ``kubectl exec`` invocado falha com ``connection timed out`` — exato bug
    que ficou em produção por 4 dias após o PR #420.
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
            has_443 = any(p.get("port") == 443 and p.get("protocol", "TCP") == "TCP" for p in ports)
            if not has_443:
                continue
            for to in to_blocks:
                ip_block = to.get("ipBlock") or {}
                if ip_block.get("cidr") == K3S_SERVICE_CIDR:
                    if covers("deile-pipeline"):
                        apiserver_egress_covered.add("deile-pipeline")
                    if covers("claude-creds-renewer"):
                        apiserver_egress_covered.add("claude-creds-renewer")

    required = {"deile-pipeline", "claude-creds-renewer"}
    missing = required - apiserver_egress_covered
    assert not missing, (
        f"Pods que invocam `kubectl exec` sem NetworkPolicy de egress para o "
        f"kube-apiserver (Service CIDR {K3S_SERVICE_CIDR}): {sorted(missing)}.\n"
        "Sem esta regra, `kubectl exec` falha com 'dial tcp 10.43.0.1:443: "
        "connect: connection timed out' (foi o bug que afetou o PR #420 em "
        "produção). Adicione/estenda uma NetworkPolicy de egress no manifest "
        "40-network-policy.yaml cobrindo esses pods. Exemplo de bloco mínimo:\n"
        "  spec:\n"
        "    podSelector:\n"
        "      matchExpressions:\n"
        "        - { key: app, operator: In, values: [deile-pipeline, "
        "claude-creds-renewer] }\n"
        "    policyTypes: [\"Egress\"]\n"
        "    egress:\n"
        "      - to: [{ ipBlock: { cidr: 10.43.0.0/16 } }]\n"
        "        ports: [{ protocol: TCP, port: 443 }]"
    )
