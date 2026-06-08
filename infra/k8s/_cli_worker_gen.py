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

import os
import sys
from pathlib import Path
from string import Template
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = _HERE / "manifests" / "templates" / "cli-worker.yaml.tmpl"
#: Diretório onde os manifests gerados são escritos por ``gen-worker``.
GENERATED_DIR = _HERE / "manifests" / "generated"

#: Forges sempre permitidas no egress (qualquer worker que faz push precisa).
_FORGE_HOSTS = ("github.com", "gitlab.com")

#: Sufixos que marcam uma var de provider como SENSÍVEL (vai pro Secret, nunca
#: literal no manifest). Complementa o casamento com ``auth_env_keys``.
_SENSITIVE_SUFFIXES = ("_API_KEY", "_TOKEN")

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


def _provider_env_prefix(kind: str) -> str:
    """Prefixo da convenção de provider-env de um *kind* (UPPERCASE)."""
    return f"DEILE_CLI_{kind.upper()}_ENV_"


def parse_provider_env(kind: str, env_source: Dict[str, str]) -> Dict[str, str]:
    """Extrai as vars de provider de *env_source* para o worker *kind* (função pura).

    Convenção: ``DEILE_CLI_<KIND>_ENV_<VARNAME>=<valor>`` vira ``<VARNAME>=<valor>``
    na env do Deployment do worker. Só considera chaves com valor não-vazio; o
    ``<VARNAME>`` precisa ser não-vazio (``DEILE_CLI_QWEN_ENV_=x`` é ignorado).
    Não lê ``os.environ`` — recebe a fonte explicitamente (testável).
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
    """True se a var de provider é sensível (vai pro Secret, não literal).

    Sensível = casa com uma ``auth_env_key`` do adapter OU termina em
    ``_API_KEY``/``_TOKEN``. Conservador por desenho: na dúvida, trata como
    segredo (não materializa no manifest).
    """
    if varname in set(getattr(adapter, "auth_env_keys", []) or []):
        return True
    return varname.upper().endswith(_SENSITIVE_SUFFIXES)


def split_provider_env(
    provider_env: Dict[str, str], adapter,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Particiona as vars de provider em ``(literais, sensíveis)`` (função pura).

    Literais → valor direto no manifest (ex.: ``OPENAI_BASE_URL``).
    Sensíveis → ``secretKeyRef`` no manifest + valor no Secret ``cli-worker-keys``.
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
    """Une ``.env`` da raiz do repo + ``os.environ`` (``os.environ`` prevalece).

    Parse simples KEY=VALUE (sem ``python-dotenv`` — infra/k8s roda standalone),
    tolerante a ausência. ``os.environ`` sobrepõe o ``.env`` (operador que
    exportou a var na sessão tem prioridade).
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
    """Resolve as vars de provider do *kind* particionadas em ``(literais, sensíveis)``.

    Lê de *env_source* quando dado (testes), senão de :func:`read_env_sources`
    (``.env`` + ``os.environ``).
    """
    source = read_env_sources() if env_source is None else env_source
    provider_env = parse_provider_env(kind, source)
    return split_provider_env(provider_env, adapter)


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


def _secret_ref_env_entry(name: str, indent: str) -> str:
    """Entrada ``env`` que lê *name* do Secret ``cli-worker-keys`` (``optional``)."""
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
    """Bloco ``env`` da config de provider por worker (convenção ``DEILE_CLI_*_ENV_*``).

    Não-sensíveis (ex.: ``OPENAI_BASE_URL``/``OPENAI_MODEL``) entram como valor
    literal; sensíveis (casam ``auth_env_keys`` ou terminam em ``_API_KEY``/
    ``_TOKEN``) entram via ``secretKeyRef`` no Secret ``cli-worker-keys`` — o
    valor NUNCA é materializado no manifest. Ausência total de
    ``DEILE_CLI_<KIND>_ENV_*`` → comentário no-op (comportamento atual inalterado).
    """
    literals, secrets = resolve_provider_env(
        kind, adapter, env_source=env_source,
    )
    # ``auth_env_keys`` já saem como ``secretKeyRef`` no AUTH_ENV_BLOCK; não
    # duplicar a entrada (env name repetido vira ruído no Deployment).
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
    """Env ``DEILE_<KIND>_AUTH=oauth`` quando o worker é renderizado em modo OAuth.

    Só emitido quando o worker roda em OAuth — ``oauth_mode=True`` (opt-in de um
    adapter oauth-capable env-default, ex.: codex) OU adapter ``oauth_file``
    estático. Espelha o ``DEILE_<KIND>_AUTH=oauth`` que o ``cli-worker-login`` já
    seta no Deployment via ``kubectl set env`` — tê-lo no manifest mantém a
    seleção do modo OAuth durável a re-aplicações do manifest. Workers env-auth
    (sem oauth_mode) não emitem nada — comportamento inalterado.
    """
    if not _is_oauth_file(adapter, oauth_mode=oauth_mode):
        return f"{indent}# (worker env-auth — sem DEILE_{kind.upper()}_AUTH)"
    return f'{indent}- {{ name: DEILE_{kind.upper()}_AUTH, value: "oauth" }}'


def _needs_pvc(adapter, *, oauth_mode: bool = False) -> bool:
    """True se o worker precisa persistir estado (oauth refresh ou resume).

    ``oauth_mode=True`` força o caminho com PVC mesmo num adapter cujo
    ``auth_mode`` default é ``env`` mas que tem ``OAuthSpec`` (opt-in via
    ``DEILE_<KIND>_AUTH=oauth`` — ex.: codex): no modo OAuth a credencial é
    refrescada in-pod e precisa do PVC para sobreviver ao restart.
    """
    return (
        _is_oauth_file(adapter, oauth_mode=oauth_mode)
        or bool(getattr(adapter, "supports_resume", False))
    )


def _is_oauth_file(adapter, *, oauth_mode: bool = False) -> bool:
    """True se o worker deve ser renderizado em modo OAuth (credencial em arquivo).

    Verdadeiro quando o adapter declara ``auth_mode="oauth_file"`` (estático) OU
    quando o chamador força ``oauth_mode=True`` — caminho opt-in de um adapter
    env-default mas oauth-capable (tem ``OAuthSpec``). O override é o que faz o
    codex (``auth_mode="env"`` + ``oauth``) renderizar PVC + initContainer +
    mount da credencial quando instalado via ``cli-worker-login``.
    """
    return oauth_mode or getattr(adapter, "auth_mode", "env") == "oauth_file"


def resolve_pod_cred_path(adapter, *, kind: str) -> str:
    """Caminho ABSOLUTO da credencial OAuth dentro do pod (função pura).

    Expande o ``~`` declarado em ``adapter.oauth.cred_path`` para o HOME do pod
    deste worker (``/home/<kind>``), que é onde o PVC ``worker-home`` monta. Um
    ``cred_path`` já absoluto é devolvido como veio. Sem ``oauth`` declarado,
    cai no default ``<home>/.<kind>/auth.json`` (conservador; o adapter deveria
    declarar o path).
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
    """Nome do Secret que carrega a credencial OAuth capturada do host.

    Reusa o ``secret_name`` declarado no :class:`OAuthSpec` do adapter quando
    presente; senão deriva ``<kind>-worker-credentials`` (convenção da frota).
    """
    oauth = getattr(adapter, "oauth", None)
    declared = getattr(oauth, "secret_name", None) if oauth else None
    if declared and str(declared).strip():
        return str(declared).strip()
    return f"{kind}-worker-credentials"


def _oauth_init_block(
    adapter, *, kind: str, indent: str = "      ", oauth_mode: bool = False,
) -> str:
    """``initContainers`` ``bootstrap-creds`` para workers ``oauth_file``.

    Espelha o initContainer do claude-worker (manifest 50): copia a credencial
    OAuth do Secret montado em ``/run/secrets/<kind>-oauth/<basename>`` para o
    PVC no path que o CLI espera (``resolve_pod_cred_path``), mode ``0600``,
    rodando como uid 10001 (PSS restricted). Idempotência por ``expiresAt``:
    preserva o token do PVC quando ele é >= o do Secret (refresh in-pod),
    copiando só quando o PVC não tem credencial OU o Secret é mais recente.

    Workers ``env`` SEM ``oauth_mode`` NÃO geram initContainer — devolve "" (no-op,
    sem regressão). Com ``oauth_mode=True`` (opt-in de um adapter oauth-capable) o
    bloco é gerado normalmente.
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
    """Volume do Secret de credencial OAuth (workers ``oauth_file``) ou "".

    O Secret é montado read-only no initContainer; o nome do Secret é resolvido
    de :func:`cred_secret_name`. Workers ``env`` sem ``oauth_mode`` não geram
    volume — devolve "".
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
    """Objeto ``PersistentVolumeClaim`` quando o worker persiste estado.

    O ``_home_volume_block`` referencia ``persistentVolumeClaim.claimName:
    <worker>-home`` para workers ``oauth_file``/``supports_resume``; sem este
    objeto a PVC ficaria *unbound* e o Pod travaria em ``Pending`` (plano §1.13).
    Workers ``emptyDir`` (env-only, sem resume) não geram PVC — devolve "".
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
    """CronJob de cleanup do PVC quando o worker tem PVC (plano §1.13).

    Reusa ``_worker_core.startup_cleanup`` (via ``cli_worker_server.run_cleanup``)
    montando o PVC ``<worker>-home``. Workers ``emptyDir`` não precisam — o
    filesystem some com o pod, e o server já faz cleanup periódico in-process;
    devolve "" para eles.
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
    oauth_mode: bool = False,
) -> str:
    """Renderiza o YAML completo (Deployment+Service+Secret+NetworkPolicy) do worker.

    Args:
        kind: kind do adapter (deve estar registrado em ``cli_adapters.ADAPTERS``).
        namespace: namespace k8s alvo (default ``deile``).
        timeout_s: timeout do subprocess; default :data:`_DEFAULT_TIMEOUT_S`.
        oauth_mode: força o caminho OAuth (PVC + initContainer ``bootstrap-creds``
            + mount da credencial + env ``DEILE_<KIND>_AUTH=oauth``) mesmo num
            adapter cujo ``auth_mode`` default é ``env`` mas que é oauth-capable
            (tem ``OAuthSpec`` — ex.: codex). Workers env-auth sem este flag
            renderizam inalterados (``emptyDir``, sem initContainer). Adapters
            ``auth_mode="oauth_file"`` renderizam OAuth independentemente do flag.

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
    """Caminho onde o manifest gerado de *kind* é escrito."""
    return GENERATED_DIR / f"{kind}-worker.yaml"


def write_manifests(
    kind: str,
    *,
    namespace: str = "deile",
    timeout_s: Optional[int] = None,
    oauth_mode: bool = False,
) -> Path:
    """Renderiza e GRAVA o manifest gerado em :func:`manifest_path`.

    Returns:
        O ``Path`` do arquivo escrito.
    """
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
