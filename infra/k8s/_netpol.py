"""Estreitamento em runtime da NetworkPolicy de egress ao kube-apiserver.

Módulo COMPARTILHADO por todos os caminhos que aplicam ``40-network-policy.yaml``
(``deploy.py`` k8s up / create-namespace / restart / start, ``_setup.py`` k8s
setup, ``_claude_install.py`` k8s claude-login). Centralizar aqui garante que
nenhum caminho deixe o fallback RFC1918 amplo no lugar — ou reverta um ``/32``
estreitado por outro caminho.

Por que existe: NetworkPolicy filtra o IP de destino *pós-DNAT* — o endpoint
REAL do apiserver — e não o ClusterIP do Service ``kubernetes`` (10.43.0.1 em
k3s). Liberar só o Service CIDR nunca casa com o pacote; o ``kubectl exec`` do
auto-renew morre em ``connection refused``. O endpoint real varia por cluster
(k3s: ``node:6443``; EKS/GKE: ``master:443``), então descobrimos em runtime via
``endpoints/kubernetes`` e estreitamos para o(s) IP(s) host (``/32`` IPv4 ou
``/128`` IPv6) + a(s) porta(s) real(is).

A policy renderizada usa o MESMO nome do fallback estático no manifest 40
(``creds-renewer-egress-to-kube-api``) DE PROPÓSITO: NetworkPolicy é aditiva
(união entre policies que selecionam o mesmo pod), então estreitar exige
SOBRESCREVER a policy por nome — um nome distinto só somaria à broad, anulando
o efeito. O selector de pods é fonte única em :data:`APISERVER_EGRESS_APPS`,
espelhado no manifest 40 e verificado por teste-guarda
(``test_k8s_manifest_consistency.py``).

Best-effort: se a descoberta falhar (endpoints vazio, IP inválido, kubectl
indisponível), mantém-se o fallback do manifest 40 — funcional, apenas mais amplo.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Pods que invocam ``kubectl exec`` e precisam de egress ao apiserver.
# FONTE ÚNICA — espelhada no `podSelector` do manifest 40 (fallback) e
# renderizada aqui (override de runtime). Divergência é pega pelo teste-guarda
# `test_apiserver_egress_selector_matches_runtime`.
APISERVER_EGRESS_APPS: List[str] = [
    "deile-pipeline",
    "claude-creds-renewer",
    "deile-monitor",
]

# Mesmo nome do fallback no manifest 40 — overwrite-by-name é o que estreita
# (ver docstring do módulo). NÃO renomear sem entender a semântica aditiva.
POLICY_NAME = "creds-renewer-egress-to-kube-api"


def _noop(_msg: str) -> None:
    pass


def _capture(cmd: List[str], timeout: float = 30.0) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def apply_apiserver_egress_netpol(
    kubectl: str,
    ns: str,
    *,
    info: Callable[[str], None] = _noop,
    warn: Callable[[str], None] = _noop,
) -> None:
    """Descobre o endpoint real do apiserver e estreita a egress policy do ns.

    Idempotente e best-effort: nunca levanta; em qualquer falha mantém o
    fallback do manifest 40. ``info``/``warn`` permitem o caller plugar sua UI
    (``deploy.py`` passa ``ui.info``/``ui.warn``); por padrão só loga.
    """
    raw = _capture([kubectl, "get", "endpoints", "kubernetes",
                    "-n", "default", "-o", "json"])
    ips: List[str] = []
    ports: List[int] = []
    if raw:
        try:
            data = json.loads(raw)
            for subset in data.get("subsets", []) or []:
                ips.extend(a["ip"] for a in subset.get("addresses", []) or [] if a.get("ip"))
                ports.extend(p["port"] for p in subset.get("ports", []) or [] if p.get("port"))
        except (ValueError, KeyError, TypeError):
            ips, ports = [], []
    ips = sorted(set(ips))
    ports = sorted(set(ports))

    # Host prefix correto por família: /32 IPv4, /128 IPv6. Ignora IP inválido.
    cidrs: List[str] = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            warn(f"endpoint apiserver inválido ignorado: {ip}")
            continue
        cidrs.append(f"{ip}/{addr.max_prefixlen}")

    if not cidrs or not ports:
        warn("endpoints/kubernetes sem IP/porta válidos — mantendo fallback "
             "RFC1918 do manifest 40")
        logger.warning("apiserver endpoints discovery vazio para ns=%s", ns)
        return

    apps = ", ".join(APISERVER_EGRESS_APPS)
    to_block = "\n".join(f"        - ipBlock: {{ cidr: {c} }}" for c in cidrs)
    ports_block = "\n".join(f"        - {{ protocol: TCP, port: {p} }}" for p in ports)
    manifest = (
        "apiVersion: networking.k8s.io/v1\n"
        "kind: NetworkPolicy\n"
        "metadata:\n"
        f"  name: {POLICY_NAME}\n"
        f"  namespace: {ns}\n"
        "spec:\n"
        "  podSelector:\n"
        "    matchExpressions:\n"
        f"      - {{ key: app, operator: In, values: [{apps}] }}\n"
        "  policyTypes: [\"Egress\"]\n"
        "  egress:\n"
        "    - to:\n"
        f"{to_block}\n"
        "      ports:\n"
        f"{ports_block}\n"
    )
    try:
        proc = subprocess.run(
            [kubectl, "apply", "-n", ns, "-f", "-"],
            input=manifest, text=True, capture_output=True,
        )
    except OSError as exc:
        warn(f"netpol apiserver-egress não aplicada: {exc}")
        return
    if proc.returncode == 0:
        info("netpol apiserver-egress estreitada para "
             f"{', '.join(cidrs)} :{','.join(map(str, ports))}")
    else:
        warn(f"netpol apiserver-egress não aplicada: {(proc.stderr or '').strip()}")
