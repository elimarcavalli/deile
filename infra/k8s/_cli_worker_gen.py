#!/usr/bin/env python3
"""_cli_worker_gen — geração de manifests de CLI worker a partir do adapter.

``deploy.py k8s gen-worker <kind>`` delega a este módulo. A regra de ouro
(plano §1.0) é que **adicionar um worker NÃO escreve YAML à mão**: o manifest é
renderizado do template ``manifests/templates/cli-worker.yaml.tmpl`` preenchendo
os placeholders ``$VAR`` a partir dos METADADOS do adapter
(``cli_adapters/<kind>.py``) — porta, env de auth, dirs graváveis, egress hosts,
storage mode. Single source of truth: o registro de adapters.

Blocos derivados do adapter:

* **AUTH_ENV_BLOCK** — para cada ``auth_env_keys`` do adapter, uma entrada
  ``env`` que lê a chave do Secret compartilhado ``cli-worker-keys`` (plano
  §1.9). Mantém ``optional: true`` para que a chave ausente não derrube o pod —
  o ``/v1/health`` reporta ``ready=false`` e o painel sinaliza.
* **OVERLAY_ENV_BLOCK** — env extras que o adapter exige por config
  (``adapter.env_overlay(home=...)``), ex.: ``OPENCODE_CONFIG_CONTENT``,
  ``GOOSE_DISABLE_KEYRING``. As ``auth_env_keys`` são REMOVIDAS daqui (vêm do
  Secret, não como valor literal) — nunca se materializa segredo no manifest.
* **HOME_VOLUME_BLOCK** — PVC ``<worker>-home`` quando o adapter precisa
  persistir estado (``auth_mode=="oauth_file"`` para refresh in-pod, ou
  ``supports_resume`` para a sessão JSONL); caso contrário ``emptyDir`` efêmero
  (mais barato), conforme o storage map do plano §1.13.
* **EGRESS_HOST_RULES / EGRESS_HOSTS_CSV** — derivados de ``adapter.egress_hosts``
  + forges. O k3s CNI não resolve FQDN, então o egress 443 é aberto (mesma
  limitação do manifest 40); os hosts ficam documentados na annotation para
  auditoria e migração futura a um CNI FQDN-aware.

Este módulo é puro (sem rede, sem kubectl): recebe metadados, devolve YAML como
string. ``deploy.py`` cuida do I/O (escrever o arquivo, aplicar no cluster).
Isso o torna trivialmente testável.
"""

from __future__ import annotations

import sys
from pathlib import Path
from string import Template
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = _HERE / "manifests" / "templates" / "cli-worker.yaml.tmpl"
#: Diretório onde os manifests gerados são escritos por ``gen-worker``.
GENERATED_DIR = _HERE / "manifests" / "generated"

#: Forges sempre permitidas no egress (qualquer worker que faz push precisa).
_FORGE_HOSTS = ("github.com", "gitlab.com")

#: Timeout default do subprocess de um CLI worker (s). CLIs não têm cap de
#: orçamento nativo (só o claude tem ``--max-budget-usd``); o controle de custo
#: é timeout + modelo barato (plano §"Controle de custo").
_DEFAULT_TIMEOUT_S = 1800


def _ensure_cli_adapters_on_path() -> None:
    """Garante que ``cli_adapters`` é importável (layout repo/dev e cluster)."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))


def load_adapter(kind: str):
    """Resolve o adapter para *kind* do registro, ou levanta ``KeyError``."""
    _ensure_cli_adapters_on_path()
    import cli_adapters  # noqa: PLC0415

    return cli_adapters.get_adapter(kind)


def available_kinds() -> List[str]:
    """Lista os kinds de CLI worker descobertos no registro (ordenados)."""
    _ensure_cli_adapters_on_path()
    import cli_adapters  # noqa: PLC0415

    return sorted(cli_adapters.ADAPTERS)


def _yaml_env_entry(name: str, value: str, indent: str) -> str:
    """Uma entrada ``env`` literal ``{name: X, value: "Y"}`` indentada."""
    # value escapado em aspas — cobre valores com ``:``/``{}`` (config inline).
    safe = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{indent}- {{ name: {name}, value: "{safe}" }}'


def _auth_env_block(adapter, *, indent: str = "            ") -> str:
    """Bloco ``env`` que lê cada ``auth_env_keys`` do Secret ``cli-worker-keys``.

    ``optional: true``: a chave ausente NÃO derruba o pod (o ``/v1/health``
    reporta ``ready=false``). Permite ao operador instalar o worker e só depois
    popular a chave — paridade com o opt-in do claude-login.
    """
    keys = list(getattr(adapter, "auth_env_keys", []) or [])
    if not keys:
        return f"{indent}# (adapter sem auth_env_keys — auth via oauth_file)"
    lines: List[str] = []
    for key in keys:
        lines.append(f"{indent}- name: {key}")
        lines.append(f"{indent}  valueFrom:")
        lines.append(f"{indent}    secretKeyRef:")
        lines.append(f"{indent}      name: cli-worker-keys")
        lines.append(f"{indent}      key: {key}")
        lines.append(f"{indent}      optional: true")
    return "\n".join(lines)


def _overlay_env_block(adapter, *, kind: str, indent: str = "            ") -> str:
    """Env extras do adapter (``env_overlay``), excluindo as ``auth_env_keys``.

    Os segredos (``auth_env_keys``) NUNCA são materializados como valor literal
    aqui — vêm do Secret (ver :func:`_auth_env_block`). Qualquer chave do overlay
    que colida com uma ``auth_env_key`` é removida defensivamente.
    """
    home = f"/home/{kind}"
    try:
        overlay = dict(adapter.env_overlay(home=home) or {})
    except Exception:  # noqa: BLE001 — overlay defensivo; pod sobe sem extras
        overlay = {}
    secret_keys = set(getattr(adapter, "auth_env_keys", []) or [])
    # HOME já é setado pelo template; não duplicar.
    entries = [
        _yaml_env_entry(k, str(v), indent)
        for k, v in overlay.items()
        if k not in secret_keys and k != "HOME"
    ]
    if not entries:
        return f"{indent}# (adapter sem env_overlay adicional)"
    return "\n".join(entries)


def _needs_pvc(adapter) -> bool:
    """True se o worker precisa persistir estado (oauth refresh ou resume)."""
    return (
        getattr(adapter, "auth_mode", "env") == "oauth_file"
        or bool(getattr(adapter, "supports_resume", False))
    )


def _home_volume_block(adapter, *, worker: str, indent: str = "        ") -> str:
    """Volume do HOME: PVC quando persiste estado, senão ``emptyDir`` efêmero."""
    if _needs_pvc(adapter):
        return (
            f"{indent}- name: worker-home\n"
            f"{indent}  persistentVolumeClaim:\n"
            f"{indent}    claimName: {worker}-home"
        )
    return (
        f"{indent}- name: worker-home\n"
        f"{indent}  emptyDir: {{ sizeLimit: \"10Gi\" }}"
    )


def egress_hosts(adapter) -> List[str]:
    """Hosts de egress do worker = LLM (adapter) ∪ forges, deduplicados/ordenados."""
    llm = [h for h in (getattr(adapter, "egress_hosts", []) or []) if h]
    merged = dict.fromkeys([*llm, *_FORGE_HOSTS])  # preserva ordem, dedup
    return list(merged)


def _egress_host_rules(adapter, *, port: int, indent: str = "    ") -> str:
    """Regra(s) de egress 443.

    O CNI do k3s não resolve FQDN, então abre-se o egress 443 para qualquer
    destino (mesma limitação aceita no manifest 40). Os hosts específicos ficam
    documentados na annotation ``deile.io/egress-llm-hosts`` (ver template). Esta
    função emite a regra ``- to: [] ports: [443]`` comentando os hosts cobertos
    para o leitor do manifest gerado.
    """
    hosts = egress_hosts(adapter)
    host_comment = ", ".join(hosts) if hosts else "(nenhum host declarado)"
    return (
        f"{indent}# hosts cobertos: {host_comment}\n"
        f"{indent}- to: []\n"
        f"{indent}  ports:\n"
        f"{indent}    - protocol: TCP\n"
        f"{indent}      port: 443"
    )


def render_manifests(
    kind: str,
    *,
    namespace: str = "deile",
    timeout_s: Optional[int] = None,
) -> str:
    """Renderiza o YAML completo (Deployment+Service+Secret+NetworkPolicy) do worker.

    Args:
        kind: kind do adapter (deve estar registrado em ``cli_adapters.ADAPTERS``).
        namespace: namespace k8s alvo (default ``deile``).
        timeout_s: timeout do subprocess; default :data:`_DEFAULT_TIMEOUT_S`.

    Returns:
        O documento multi-YAML como string, pronto para escrever/``kubectl apply``.

    Raises:
        KeyError: *kind* não está registrado.
        FileNotFoundError: o template não foi encontrado.
    """
    adapter = load_adapter(kind)
    port = int(getattr(adapter, "default_port", 0) or 0)
    if port <= 0:
        raise ValueError(
            f"adapter {kind!r} sem default_port válido ({port!r}) — "
            "não dá para gerar manifest sem porta"
        )
    worker = f"{kind}-worker"
    if not TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"template não encontrado: {TEMPLATE_PATH}")

    tmpl = Template(TEMPLATE_PATH.read_text(encoding="utf-8"))
    mapping: Dict[str, str] = {
        "KIND": kind,
        "WORKER": worker,
        "PORT": str(port),
        "NAMESPACE": namespace,
        "TIMEOUT_S": str(timeout_s or _DEFAULT_TIMEOUT_S),
        "AUTH_ENV_BLOCK": _auth_env_block(adapter),
        "OVERLAY_ENV_BLOCK": _overlay_env_block(adapter, kind=kind),
        "HOME_VOLUME_BLOCK": _home_volume_block(adapter, worker=worker),
        "EGRESS_HOST_RULES": _egress_host_rules(adapter, port=port),
        "EGRESS_HOSTS_CSV": ", ".join(egress_hosts(adapter)),
    }
    return tmpl.substitute(mapping)


def manifest_path(kind: str) -> Path:
    """Caminho onde o manifest gerado de *kind* é escrito."""
    return GENERATED_DIR / f"{kind}-worker.yaml"


def write_manifests(
    kind: str,
    *,
    namespace: str = "deile",
    timeout_s: Optional[int] = None,
) -> Path:
    """Renderiza e GRAVA o manifest gerado em :func:`manifest_path`.

    Returns:
        O ``Path`` do arquivo escrito.
    """
    rendered = render_manifests(kind, namespace=namespace, timeout_s=timeout_s)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out = manifest_path(kind)
    out.write_text(rendered, encoding="utf-8")
    return out


__all__ = [
    "TEMPLATE_PATH",
    "GENERATED_DIR",
    "available_kinds",
    "load_adapter",
    "egress_hosts",
    "render_manifests",
    "manifest_path",
    "write_manifests",
]
