#!/usr/bin/env python3
"""_cli_worker_gen — geração de manifests de CLI worker a partir do adapter.

``deploy.py k8s gen-worker <kind>`` renderiza o template
``manifests/templates/cli-worker.yaml.tmpl`` preenchendo os placeholders ``$VAR``
com os metadados do adapter (porta, env de auth, dirs graváveis, egress hosts,
storage mode). Single source of truth: o registro de adapters em
``cli_adapters/<kind>.py``.

HOME_VOLUME_BLOCK usa PVC quando o adapter precisa persistir estado
(``auth_mode=="oauth_file"`` para refresh in-pod ou ``supports_resume`` para a
sessão JSONL); caso contrário, ``emptyDir`` efêmero (plano §1.13).

Módulo puro (sem rede, sem kubectl): recebe metadados, devolve YAML como string.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from string import Template
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = _HERE / "manifests" / "templates" / "cli-worker.yaml.tmpl"
GENERATED_DIR = _HERE / "manifests" / "generated"

#: Forges sempre presentes no egress de qualquer worker que faz push.
_FORGE_HOSTS = ("github.com", "gitlab.com")

#: Vars de provider sensíveis — vão pro Secret, nunca literais no manifest.
_SENSITIVE_SUFFIXES = ("_API_KEY", "_TOKEN")

#: CLIs não têm cap de orçamento nativo (só o claude tem ``--max-budget-usd``);
#: timeout é o único controle de custo (plano §"Controle de custo").
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


def _provider_env_prefix(kind: str) -> str:
    return f"DEILE_CLI_{kind.upper()}_ENV_"


def parse_provider_env(kind: str, env_source: Dict[str, str]) -> Dict[str, str]:
    """Extrai vars ``DEILE_CLI_<KIND>_ENV_<VARNAME>`` → ``{VARNAME: valor}`` (função pura).

    ``<VARNAME>`` vazio (``DEILE_CLI_QWEN_ENV_=x``) é ignorado. Não lê
    ``os.environ`` — recebe a fonte explicitamente para ser testável.
    """
    prefix = _provider_env_prefix(kind)
    out: Dict[str, str] = {}
    for key, val in env_source.items():
        if not key.startswith(prefix):
            continue
        varname = key[len(prefix):].strip()
        value = (val or "").strip()
        if varname and value:
            out[varname] = value
    return out


def _is_sensitive_provider_var(varname: str, adapter) -> bool:
    """True se a var é sensível (casa ``auth_env_keys`` ou sufixo ``_API_KEY``/``_TOKEN``).

    Conservador: na dúvida trata como segredo — não materializa no manifest.
    """
    return (
        varname in set(getattr(adapter, "auth_env_keys", []) or [])
        or varname.upper().endswith(_SENSITIVE_SUFFIXES)
    )


def split_provider_env(
    provider_env: Dict[str, str], adapter,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Particiona vars de provider em ``(literais, sensíveis)`` (função pura).

    Literais → valor direto no manifest. Sensíveis → ``secretKeyRef`` em
    ``cli-worker-keys``.
    """
    literals: Dict[str, str] = {}
    secrets: Dict[str, str] = {}
    for varname, value in provider_env.items():
        if _is_sensitive_provider_var(varname, adapter):
            secrets[varname] = value
        else:
            literals[varname] = value
    return literals, secrets


def read_env_sources() -> Dict[str, str]:
    """Une ``.env`` (raiz do repo) + ``os.environ``; ``os.environ`` prevalece.

    Parse simples KEY=VALUE sem ``python-dotenv`` — infra/k8s roda standalone.
    """
    merged: Dict[str, str] = {}
    env_file = _HERE.parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            merged[key.strip()] = val.strip().strip('"').strip("'")
    for key, val in os.environ.items():
        if key.startswith("DEILE_CLI_"):
            merged[key] = val
    return merged


def resolve_provider_env(
    kind: str, adapter, *, env_source: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Vars de provider do *kind* particionadas em ``(literais, sensíveis)``.

    Usa *env_source* quando fornecido (testes); senão :func:`read_env_sources`.
    """
    source = read_env_sources() if env_source is None else env_source
    provider_env = parse_provider_env(kind, source)
    return split_provider_env(provider_env, adapter)


def _yaml_env_entry(name: str, value: str, indent: str) -> str:
    """Entrada ``env`` literal ``{name: X, value: "Y"}`` indentada."""
    # Escapa em aspas duplas — cobre values com ``:``/``{}`` (config inline).
    safe = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{indent}- {{ name: {name}, value: "{safe}" }}'


def _auth_env_block(adapter, *, indent: str = "            ") -> str:
    """Bloco ``env`` que lê ``auth_env_keys`` do Secret ``cli-worker-keys``.

    ``optional: true`` — chave ausente não derruba o pod; ``/v1/health`` reporta
    ``ready=false``. Permite instalar o worker antes de popular a chave.
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
    """Env extras de ``adapter.env_overlay``, excluindo ``auth_env_keys`` e ``HOME``.

    ``auth_env_keys`` vêm do Secret via :func:`_auth_env_block`; colisões são
    removidas defensivamente para nunca materializar segredo como valor literal.
    """
    home = f"/home/{kind}"
    try:
        overlay = dict(adapter.env_overlay(home=home) or {})
    except Exception:  # noqa: BLE001 — overlay defensivo; pod sobe sem extras
        overlay = {}
    secret_keys = set(getattr(adapter, "auth_env_keys", []) or [])
    # HOME é definido pelo template — não duplicar.
    entries = [
        _yaml_env_entry(k, str(v), indent)
        for k, v in overlay.items()
        if k not in secret_keys and k != "HOME"
    ]
    if not entries:
        return f"{indent}# (adapter sem env_overlay adicional)"
    return "\n".join(entries)


def _secret_ref_env_entry(name: str, indent: str) -> str:
    """Entrada ``env`` via ``secretKeyRef`` em ``cli-worker-keys`` (``optional``)."""
    return (
        f"{indent}- name: {name}\n"
        f"{indent}  valueFrom:\n"
        f"{indent}    secretKeyRef:\n"
        f"{indent}      name: cli-worker-keys\n"
        f"{indent}      key: {name}\n"
        f"{indent}      optional: true"
    )


def _provider_env_block(
    adapter,
    *,
    kind: str,
    env_source: Optional[Dict[str, str]] = None,
    indent: str = "            ",
) -> str:
    """Bloco ``env`` das vars ``DEILE_CLI_<KIND>_ENV_*`` do worker.

    Não-sensíveis → valor literal no manifest. Sensíveis (``auth_env_keys`` ou
    sufixo ``_API_KEY``/``_TOKEN``) → ``secretKeyRef`` em ``cli-worker-keys``; o
    valor nunca é materializado. Sem vars → comentário no-op.
    """
    literals, secrets = resolve_provider_env(
        kind, adapter, env_source=env_source,
    )
    # auth_env_keys já entram via AUTH_ENV_BLOCK; deduplicar evita env name
    # repetido no Deployment.
    auth_keys = set(getattr(adapter, "auth_env_keys", []) or [])
    secrets = {k: v for k, v in secrets.items() if k not in auth_keys}
    if not literals and not secrets:
        return f"{indent}# (sem provider-env DEILE_CLI_{kind.upper()}_ENV_*)"
    lines: List[str] = []
    for name in sorted(literals):
        lines.append(_yaml_env_entry(name, literals[name], indent))
    for name in sorted(secrets):
        lines.append(_secret_ref_env_entry(name, indent))
    return "\n".join(lines)


def _auth_mode_env_block(
    adapter, *, kind: str, indent: str = "            ", oauth_mode: bool = False,
) -> str:
    """Emite ``DEILE_<KIND>_AUTH=oauth`` quando o worker roda em modo OAuth.

    Espelha o que ``cli-worker-login`` seta via ``kubectl set env`` — tê-lo no
    manifest garante que uma re-aplicação não desfaça o modo OAuth. Workers
    env-auth sem ``oauth_mode`` não emitem nada.
    """
    if not _is_oauth_file(adapter, oauth_mode=oauth_mode):
        return f"{indent}# (worker env-auth — sem DEILE_{kind.upper()}_AUTH)"
    return f'{indent}- {{ name: DEILE_{kind.upper()}_AUTH, value: "oauth" }}'


def _needs_pvc(adapter, *, oauth_mode: bool = False) -> bool:
    """True se o worker precisa de PVC (oauth refresh in-pod ou resume de sessão).

    ``oauth_mode=True`` força PVC mesmo quando ``auth_mode=="env"`` — no modo
    OAuth a credencial é refrescada in-pod e precisa sobreviver ao restart.
    """
    return (
        _is_oauth_file(adapter, oauth_mode=oauth_mode)
        or bool(getattr(adapter, "supports_resume", False))
    )


def _is_oauth_file(adapter, *, oauth_mode: bool = False) -> bool:
    """True quando o worker usa credencial em arquivo (``auth_mode="oauth_file"`` ou ``oauth_mode=True``).

    ``oauth_mode=True`` é o opt-in para adapters env-default mas oauth-capable
    (ex.: codex instalado via ``cli-worker-login``).
    """
    return oauth_mode or getattr(adapter, "auth_mode", "env") == "oauth_file"


def resolve_pod_cred_path(adapter, *, kind: str) -> str:
    """Caminho absoluto da credencial OAuth no pod (função pura).

    Expande ``~`` de ``adapter.oauth.cred_path`` para ``/home/<kind>`` (onde o
    PVC monta). Path já absoluto é devolvido como veio. Sem ``oauth`` declarado,
    usa ``/home/<kind>/.<kind>/auth.json`` como fallback.
    """
    home = f"/home/{kind}"
    oauth = getattr(adapter, "oauth", None)
    cred_path = getattr(oauth, "cred_path", None) if oauth else None
    if not cred_path:
        return f"{home}/.{kind}/auth.json"
    cred_path = cred_path.strip()
    if cred_path.startswith("~/"):
        return f"{home}/{cred_path[2:]}"
    if cred_path == "~":
        return home
    if cred_path.startswith("/"):
        return cred_path
    return f"{home}/{cred_path}"


def cred_secret_name(adapter, *, kind: str) -> str:
    """Nome do Secret OAuth: ``adapter.oauth.secret_name`` ou ``<kind>-worker-credentials``."""
    oauth = getattr(adapter, "oauth", None)
    declared = getattr(oauth, "secret_name", None) if oauth else None
    if declared and str(declared).strip():
        return str(declared).strip()
    return f"{kind}-worker-credentials"


def _oauth_init_block(
    adapter, *, kind: str, indent: str = "      ", oauth_mode: bool = False,
) -> str:
    """``initContainers bootstrap-creds`` para workers ``oauth_file`` (espelha manifest 50).

    Copia a credencial OAuth do Secret (``/run/secrets/<kind>-oauth/``) para o PVC
    em ``resolve_pod_cred_path``, mode ``0600``, uid 10001. Idempotência por
    ``expiresAt``: preserva o token do PVC quando já é mais recente (refresh
    in-pod). Workers env-auth sem ``oauth_mode`` devolvem "".
    """
    if not _is_oauth_file(adapter, oauth_mode=oauth_mode):
        return ""
    cred_pod = resolve_pod_cred_path(adapter, kind=kind)
    basename = cred_pod.rsplit("/", 1)[-1]
    cred_dir = cred_pod.rsplit("/", 1)[0] if "/" in cred_pod else f"/home/{kind}"
    secret_mount = f"/run/secrets/{kind}-oauth"
    cred_secret = f"{secret_mount}/{basename}"
    py_read_exp = (
        'import json,sys;d=json.load(open(sys.argv[1]));'
        'o=d.get("claudeAiOauth") or d.get("tokens") or {};'
        'print(int(o.get("expiresAt") or o.get("expires_at") '
        'or d.get("expiresAt") or 0))'
    )
    script = "\n".join(
        f"{indent}        {ln}"
        for ln in (
            "set -eu",
            f'mkdir -p "{cred_dir}"',
            f'CREDS_PVC="{cred_pod}"',
            f'CREDS_SECRET="{cred_secret}"',
            f"PY_READ_EXP='{py_read_exp}'",
            'if [ ! -f "$CREDS_PVC" ]; then',
            '  echo "bootstrap-creds: PVC sem credencial - copiando do Secret"',
            '  cp "$CREDS_SECRET" "$CREDS_PVC"',
            '  chmod 0600 "$CREDS_PVC"',
            "else",
            '  PVC_EXP=$(python3 -c "$PY_READ_EXP" "$CREDS_PVC" 2>/dev/null || echo 0)',
            '  SECRET_EXP=$(python3 -c "$PY_READ_EXP" "$CREDS_SECRET" 2>/dev/null || echo 0)',
            '  if [ "${SECRET_EXP:-0}" -gt "${PVC_EXP:-0}" ]; then',
            '    echo "bootstrap-creds: Secret mais recente - copiando"',
            '    cp "$CREDS_SECRET" "$CREDS_PVC"',
            '    chmod 0600 "$CREDS_PVC"',
            "  else",
            '    echo "bootstrap-creds: PVC preservado (refresh in-pod)"',
            "  fi",
            "fi",
            'ls -la "$CREDS_PVC"',
        )
    )
    return "\n".join((
        f"{indent}initContainers:",
        f"{indent}  - name: bootstrap-creds",
        f"{indent}    image: deile-cli-worker-{kind}:local",
        f"{indent}    imagePullPolicy: Never",
        f'{indent}    command: ["/bin/sh", "-c"]',
        f"{indent}    args:",
        f"{indent}      - |",
        script,
        f"{indent}    volumeMounts:",
        f"{indent}      - {{ name: oauth-cred, mountPath: {secret_mount}, readOnly: true }}",
        f"{indent}      - {{ name: worker-home, mountPath: /home/{kind} }}",
        f"{indent}    securityContext:",
        f"{indent}      runAsNonRoot: true",
        f"{indent}      runAsUser: 10001",
        f"{indent}      runAsGroup: 10001",
        f"{indent}      allowPrivilegeEscalation: false",
        f"{indent}      readOnlyRootFilesystem: true",
        f'{indent}      capabilities: {{ drop: ["ALL"] }}',
        f"{indent}      seccompProfile: {{ type: RuntimeDefault }}",
    ))


def _oauth_volume_block(
    adapter, *, kind: str, indent: str = "        ", oauth_mode: bool = False,
) -> str:
    """Volume do Secret de credencial OAuth, montado read-only no initContainer.

    Workers env-auth sem ``oauth_mode`` devolvem "".
    """
    if not _is_oauth_file(adapter, oauth_mode=oauth_mode):
        return ""
    secret = cred_secret_name(adapter, kind=kind)
    return (
        f"{indent}- name: oauth-cred\n"
        f"{indent}  secret:\n"
        f"{indent}    secretName: {secret}\n"
        f"{indent}    defaultMode: 0o400"
    )


def _home_volume_block(
    adapter, *, worker: str, indent: str = "        ", oauth_mode: bool = False,
) -> str:
    """Volume do HOME: PVC quando persiste estado, senão ``emptyDir`` efêmero."""
    if _needs_pvc(adapter, oauth_mode=oauth_mode):
        return (
            f"{indent}- name: worker-home\n"
            f"{indent}  persistentVolumeClaim:\n"
            f"{indent}    claimName: {worker}-home"
        )
    return (
        f"{indent}- name: worker-home\n"
        f"{indent}  emptyDir: {{ sizeLimit: \"10Gi\" }}"
    )


def _pvc_doc_block(
    adapter, *, worker: str, namespace: str, oauth_mode: bool = False,
) -> str:
    """Objeto ``PersistentVolumeClaim`` para workers com PVC (plano §1.13).

    Sem este objeto o pod travaria em ``Pending`` (PVC unbound). Workers
    ``emptyDir`` devolvem "".
    """
    if not _needs_pvc(adapter, oauth_mode=oauth_mode):
        return ""
    return (
        "---\n"
        "apiVersion: v1\n"
        "kind: PersistentVolumeClaim\n"
        "metadata:\n"
        f"  name: {worker}-home\n"
        f"  namespace: {namespace}\n"
        "  labels:\n"
        f"    app: {worker}\n"
        "spec:\n"
        "  accessModes: [ReadWriteOnce]\n"
        "  resources:\n"
        "    requests:\n"
        "      storage: 10Gi\n"
    )


def _cleanup_cronjob_block(
    adapter, *, kind: str, worker: str, namespace: str, oauth_mode: bool = False,
) -> str:
    """CronJob diário de GC do PVC via ``cli_worker_server.run_cleanup`` (plano §1.13).

    Workers ``emptyDir`` não precisam — o filesystem some com o pod. Devolve "".
    """
    if not _needs_pvc(adapter, oauth_mode=oauth_mode):
        return ""
    return (
        "---\n"
        "# CronJob de GC do PVC (issue #445) — monta o PVC e roda o cleanup do\n"
        "# core. Só gerado para workers com PVC (oauth_file/supports_resume).\n"
        "apiVersion: batch/v1\n"
        "kind: CronJob\n"
        "metadata:\n"
        f"  name: {worker}-cleanup\n"
        f"  namespace: {namespace}\n"
        "  labels:\n"
        f"    app: {worker}-cleanup\n"
        "    role: deile\n"
        "spec:\n"
        '  schedule: "0 3 * * *"\n'
        "  successfulJobsHistoryLimit: 3\n"
        "  failedJobsHistoryLimit: 3\n"
        "  concurrencyPolicy: Forbid\n"
        "  jobTemplate:\n"
        "    spec:\n"
        "      ttlSecondsAfterFinished: 86400\n"
        "      template:\n"
        "        metadata:\n"
        "          labels:\n"
        f"            app: {worker}-cleanup\n"
        "            role: deile\n"
        "        spec:\n"
        "          restartPolicy: OnFailure\n"
        "          automountServiceAccountToken: false\n"
        "          enableServiceLinks: false\n"
        "          securityContext:\n"
        "            runAsNonRoot: true\n"
        "            runAsUser: 10001\n"
        "            runAsGroup: 10001\n"
        "            fsGroup: 10001\n"
        '            fsGroupChangePolicy: "OnRootMismatch"\n'
        "            seccompProfile: { type: RuntimeDefault }\n"
        "          containers:\n"
        "            - name: cleanup\n"
        f"              image: deile-cli-worker-{kind}:local\n"
        "              imagePullPolicy: Never\n"
        '              command: ["python3", "-c"]\n'
        "              args:\n"
        "                - |\n"
        "                  import sys, logging\n"
        '                  sys.path.insert(0, "/app/infra/k8s")\n'
        '                  sys.path.insert(0, "/app")\n'
        "                  logging.basicConfig(\n"
        '                      level="INFO",\n'
        '                      format="%(asctime)s %(levelname)s %(name)s: %(message)s",\n'
        "                      stream=sys.stdout,\n"
        "                  )\n"
        "                  from cli_worker_server import run_cleanup\n"
        "                  r = run_cleanup()\n"
        '                  print(f"cleanup: {r}")\n'
        "                  sys.exit(1 if r['errors'] else 0)\n"
        "              env:\n"
        f'                - {{ name: DEILE_CLI_WORKER_KIND, value: "{kind}" }}\n'
        f'                - {{ name: DEILE_CLI_WORKER_ROOT, value: "/home/{kind}/work" }}\n'
        f'                - {{ name: HOME, value: "/home/{kind}" }}\n'
        "              volumeMounts:\n"
        f"                - {{ name: worker-home, mountPath: /home/{kind} }}\n"
        "              resources:\n"
        '                requests: { cpu: "50m", memory: "64Mi" }\n'
        '                limits:   { cpu: "500m", memory: "256Mi" }\n'
        "              securityContext:\n"
        "                allowPrivilegeEscalation: false\n"
        "                readOnlyRootFilesystem: true\n"
        "                runAsNonRoot: true\n"
        "                runAsUser: 10001\n"
        "                runAsGroup: 10001\n"
        '                capabilities: { drop: ["ALL"] }\n'
        "                seccompProfile: { type: RuntimeDefault }\n"
        "          volumes:\n"
        "            - name: worker-home\n"
        "              persistentVolumeClaim:\n"
        f"                claimName: {worker}-home\n"
    )


def egress_hosts(adapter) -> List[str]:
    """Hosts de egress = LLM (adapter) ∪ forges, deduplicados preservando ordem."""
    llm = [h for h in (getattr(adapter, "egress_hosts", []) or []) if h]
    merged = dict.fromkeys([*llm, *_FORGE_HOSTS])  # dedup preservando ordem
    return list(merged)


def _egress_host_rules(adapter, *, port: int, indent: str = "    ") -> str:
    """Regra de egress 443 (``to: []`` — k3s CNI não resolve FQDN, mesma limitação do manifest 40).

    Hosts cobertos ficam documentados na annotation ``deile.io/egress-llm-hosts``
    e como comentário inline no manifest gerado.
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
    oauth_mode: bool = False,
) -> str:
    """Renderiza o YAML completo (Deployment+Service+Secret+NetworkPolicy) para *kind*.

    ``oauth_mode=True`` força PVC + initContainer + env ``DEILE_<KIND>_AUTH=oauth``
    em adapters env-default mas oauth-capable (ex.: codex). Adapters
    ``auth_mode="oauth_file"`` renderizam OAuth independentemente deste flag.

    Raises:
        KeyError: *kind* não registrado.
        FileNotFoundError: template ausente.
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
        "AUTH_MODE_ENV_BLOCK": _auth_mode_env_block(
            adapter, kind=kind, oauth_mode=oauth_mode,
        ),
        "OVERLAY_ENV_BLOCK": _overlay_env_block(adapter, kind=kind),
        "PROVIDER_ENV_BLOCK": _provider_env_block(adapter, kind=kind),
        "HOME_VOLUME_BLOCK": _home_volume_block(
            adapter, worker=worker, oauth_mode=oauth_mode,
        ),
        "OAUTH_INIT_BLOCK": _oauth_init_block(
            adapter, kind=kind, oauth_mode=oauth_mode,
        ),
        "OAUTH_VOLUME_BLOCK": _oauth_volume_block(
            adapter, kind=kind, oauth_mode=oauth_mode,
        ),
        "EGRESS_HOST_RULES": _egress_host_rules(adapter, port=port),
        "EGRESS_HOSTS_CSV": ", ".join(egress_hosts(adapter)),
        "PVC_DOC_BLOCK": _pvc_doc_block(
            adapter, worker=worker, namespace=namespace, oauth_mode=oauth_mode,
        ),
        "CLEANUP_CRONJOB_BLOCK": _cleanup_cronjob_block(
            adapter, kind=kind, worker=worker, namespace=namespace,
            oauth_mode=oauth_mode,
        ),
    }
    return tmpl.substitute(mapping)


def manifest_path(kind: str) -> Path:
    """Caminho do manifest gerado para *kind*."""
    return GENERATED_DIR / f"{kind}-worker.yaml"


def write_manifests(
    kind: str,
    *,
    namespace: str = "deile",
    timeout_s: Optional[int] = None,
    oauth_mode: bool = False,
) -> Path:
    """Renderiza e grava o manifest em :func:`manifest_path`; devolve o ``Path``."""
    rendered = render_manifests(
        kind, namespace=namespace, timeout_s=timeout_s, oauth_mode=oauth_mode,
    )
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
    "resolve_pod_cred_path",
    "cred_secret_name",
    "parse_provider_env",
    "split_provider_env",
    "read_env_sources",
    "resolve_provider_env",
    "render_manifests",
    "manifest_path",
    "write_manifests",
]
