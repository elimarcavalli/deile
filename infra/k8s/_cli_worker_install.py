"""install_cli_worker — instala/desinstala um CLI worker da frota ON-DEMAND.

Generaliza o padrão do ``claude-login`` (``_claude_install``) para os workers
de CLI ``auth_mode="env"`` (opencode, aider, goose, qwen, codex no modo API
key): captura as chaves de API do ``.env`` → Secret compartilhado
``cli-worker-keys``, sincroniza o bearer (reusa o ``worker-bearer`` do
deile-worker), gera o manifest do template (``_cli_worker_gen``), aplica e
escala a 1 réplica.

**Invariante:** este módulo NUNCA é chamado por ``k8s up`` — só pelo verb
``deploy.py k8s cli-worker-install`` e pelo painel quando o operador escolhe
instalar um worker. A frota é 100% opt-in; os Deployments nascem ``replicas:0``
no manifest gerado e só sobem quando instalados aqui (ou via ``k8s scale``).

Diferença essencial vs claude-login: workers ``env`` NÃO têm login OAuth no
host — não abrem browser, não capturam credencial de arquivo. A "credencial" é
a chave de API que já vive no ``.env`` do operador; o install só a propaga ao
Secret. Workers ``oauth_file`` (futuro: codex OAuth) reusariam o mecanismo de
captura de cred do ``_claude_install`` via ``adapter.oauth`` — fora do escopo
deste módulo (env-only), registrado como caminho declarado no adapter.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent


@dataclass
class CliWorkerInstallResult:
    """Resultado de :func:`install_cli_worker` — status + flags por etapa."""

    ok: bool
    kind: str = ""
    keys_secret_applied: bool = False
    bearer_applied: bool = False
    manifest_applied: bool = False
    scaled: bool = False
    error: Optional[str] = None
    missing_keys: Optional[List[str]] = None


def _ensure_on_path() -> None:
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))


def _adapter(kind: str):
    _ensure_on_path()
    import cli_adapters  # noqa: PLC0415

    return cli_adapters.get_adapter(kind)


def _read_env_file() -> Dict[str, str]:
    """Lê o ``.env`` da raiz do repo (KEY=VALUE), tolerante a ausência.

    Não usa ``python-dotenv`` (infra/k8s roda standalone); parse simples
    suficiente para extrair as chaves de API. Também consulta ``os.environ``
    como fallback (operador que exportou a chave em vez de gravar no .env).
    """
    env: Dict[str, str] = {}
    env_file = _HERE.parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _resolve_auth_keys(adapter) -> Dict[str, str]:
    """Resolve os valores das ``auth_env_keys`` do adapter (env file > os.environ).

    Retorna só as chaves COM valor — chaves ausentes ficam de fora (o Secret é
    populado parcialmente; o ``/v1/health`` reporta ``ready=false`` até a chave
    aparecer, paridade com o opt-in do claude).
    """
    env_file = _read_env_file()
    resolved: Dict[str, str] = {}
    for key in getattr(adapter, "auth_env_keys", []) or []:
        val = (env_file.get(key) or os.environ.get(key, "")).strip()
        if val:
            resolved[key] = val
    return resolved


def _kubectl_apply_keys_secret(
    values: Dict[str, str], *, namespace: str
) -> bool:
    """Cria/atualiza (merge) o Secret compartilhado ``cli-worker-keys``.

    Faz merge: lê as chaves já presentes no Secret e sobrepõe as novas, para
    que instalar um segundo worker não apague as chaves do primeiro. Idempotente
    (dry-run|apply). Sem valores → no-op com ``True`` (o worker sobe ``not
    ready`` até a chave existir).
    """
    if not values:
        logger.warning(
            "cli-worker-keys: nenhuma chave de API resolvida — Secret não "
            "atualizado (worker subirá not-ready até a chave existir)"
        )
        return True

    merged: Dict[str, str] = {}
    # Preserva chaves já existentes (merge, não overwrite).
    try:
        existing = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", "cli-worker-keys",
             "-o", "jsonpath={.data}"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            import base64  # noqa: PLC0415
            import json  # noqa: PLC0415
            data = json.loads(existing.stdout)
            for k, b64 in (data or {}).items():
                try:
                    merged[k] = base64.b64decode(b64).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    continue
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass  # Secret ausente/illegível — começa do zero com os novos valores.
    merged.update(values)

    literals: List[str] = []
    for k, v in merged.items():
        literals.append(f"--from-literal={k}={v}")
    try:
        dry = subprocess.run(
            ["kubectl", "create", "secret", "generic", "cli-worker-keys",
             *literals, "-n", namespace, "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("dry-run cli-worker-keys falhou: %s", exc)
        return False
    if dry.returncode != 0:
        logger.error("dry-run cli-worker-keys falhou: %s", dry.stderr)
        return False
    try:
        apply = subprocess.run(
            ["kubectl", "-n", namespace, "apply", "-f", "-"],
            input=dry.stdout, capture_output=True, text=True,
            check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.error("apply cli-worker-keys timed out")
        return False
    return apply.returncode == 0


def _kubectl_sync_bearer(worker: str, *, namespace: str) -> bool:
    """Popula ``<worker>-bearer`` reusando o token do ``worker-bearer``.

    Mesma justificativa do claude-worker: o cliente do pipeline envia o mesmo
    Bearer para qualquer worker; reusar o token mantém todos atrás da mesma
    boundary (já coberta pela NetworkPolicy ingress whitelist do pipeline).
    Idempotente. Se ``worker-bearer`` ainda não existe (cluster sem ``k8s up``),
    loga warning e retorna ``True`` (o rollout fica pending até o secret existir).
    """
    try:
        get = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", "worker-bearer",
             "-o", "jsonpath={.data.AUTH_TOKEN}"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("kubectl get worker-bearer falhou: %s", exc)
        return False
    if get.returncode != 0 or not get.stdout.strip():
        logger.warning(
            "secret worker-bearer ausente — rode `deploy.py k8s up` antes de "
            "instalar um CLI worker (rollout fica pending até o secret existir)"
        )
        return True

    import base64  # noqa: PLC0415
    try:
        token = base64.b64decode(get.stdout.strip()).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("worker-bearer AUTH_TOKEN não é base64 ascii: %s", exc)
        return False

    try:
        dry = subprocess.run(
            ["kubectl", "create", "secret", "generic", f"{worker}-bearer",
             f"--from-literal=CLI_WORKER_BEARER_TOKEN={token}",
             "-n", namespace, "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("dry-run %s-bearer falhou: %s", worker, exc)
        return False
    if dry.returncode != 0:
        logger.error("dry-run %s-bearer falhou: %s", worker, dry.stderr)
        return False
    try:
        apply = subprocess.run(
            ["kubectl", "-n", namespace, "apply", "-f", "-"],
            input=dry.stdout, capture_output=True, text=True,
            check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.error("apply %s-bearer timed out", worker)
        return False
    return apply.returncode == 0


def _kubectl_apply_manifest(kind: str, *, namespace: str) -> bool:
    """Gera o manifest do template e o aplica no cluster (kubectl apply -f -).

    O Secret ``<worker>-bearer`` do manifest é um STUB (stringData vazio); ele é
    aplicado ANTES por :func:`_kubectl_sync_bearer` com o token real, então
    aplicamos o manifest gerado SEM o documento do Secret para não zerar o token
    (mesmo cuidado do claude-worker-bearer no ``k8s up``).
    """
    _ensure_on_path()
    from _cli_worker_gen import render_manifests  # noqa: PLC0415

    rendered = render_manifests(kind, namespace=namespace)
    # Remove o doc do Secret-stub do YAML aplicado (token real já está no
    # cluster via _kubectl_sync_bearer). Split por separador YAML.
    docs = rendered.split("\n---\n")
    kept = [d for d in docs if "kind: Secret" not in d]
    payload = "\n---\n".join(kept)
    try:
        apply = subprocess.run(
            ["kubectl", "-n", namespace, "apply", "-f", "-"],
            input=payload, capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("apply manifest %s-worker falhou: %s", kind, exc)
        return False
    if apply.returncode != 0:
        logger.error("apply manifest %s-worker falhou: %s", kind, apply.stderr)
        return False
    return True


def _kubectl_scale(worker: str, replicas: int, *, namespace: str) -> bool:
    """``kubectl scale deployment/<worker> --replicas=N``. Idempotente."""
    try:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "scale",
             f"deployment/{worker}", f"--replicas={replicas}"],
            capture_output=True, text=True, check=False, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("scale %s falhou: %s", worker, exc)
        return False
    return result.returncode == 0


def install_cli_worker(
    kind: str, *, namespace: str = "deile", replicas: int = 1,
) -> CliWorkerInstallResult:
    """Instala um CLI worker da frota ON-DEMAND (env-auth).

    Sequência (cada etapa idempotente):

    1. resolve o adapter (``KeyError`` se kind desconhecido → resultado de erro);
    2. resolve as ``auth_env_keys`` do ``.env`` → Secret ``cli-worker-keys``;
    3. sincroniza ``<worker>-bearer`` (reusa o token do ``worker-bearer``);
    4. gera + aplica o manifest (Deployment/Service/NetworkPolicy);
    5. escala a ``replicas`` (default 1 — sobe o worker).

    Não bloqueia esperando rollout (paridade com o painel: a UI mostra o status
    no próximo tick). Workers ``oauth_file`` NÃO são cobertos aqui (env-only);
    para eles o caminho é o ``<kind>-login`` (captura de cred OAuth do host).
    """
    try:
        adapter = _adapter(kind)
    except KeyError as exc:
        return CliWorkerInstallResult(ok=False, kind=kind, error=str(exc))

    if getattr(adapter, "auth_mode", "env") != "env":
        return CliWorkerInstallResult(
            ok=False, kind=kind,
            error=(
                f"adapter {kind!r} usa auth_mode={adapter.auth_mode!r}; "
                "install on-demand cobre só workers env-auth. Para OAuth use "
                f"o fluxo `<kind>-login` (captura de cred do host)."
            ),
        )

    worker = f"{kind}-worker"
    result = CliWorkerInstallResult(ok=False, kind=kind)

    # 2. chaves de API → Secret compartilhado.
    auth_values = _resolve_auth_keys(adapter)
    declared = list(getattr(adapter, "auth_env_keys", []) or [])
    result.missing_keys = [k for k in declared if k not in auth_values]
    if not _kubectl_apply_keys_secret(auth_values, namespace=namespace):
        result.error = "falha ao aplicar Secret cli-worker-keys"
        return result
    result.keys_secret_applied = True

    # 3. bearer.
    if not _kubectl_sync_bearer(worker, namespace=namespace):
        result.error = f"falha ao sincronizar {worker}-bearer"
        return result
    result.bearer_applied = True

    # 4. manifest.
    try:
        if not _kubectl_apply_manifest(kind, namespace=namespace):
            result.error = f"falha ao aplicar manifest de {worker}"
            return result
    except Exception as exc:  # noqa: BLE001 — render/apply pode estourar
        result.error = f"erro ao gerar/aplicar manifest: {exc}"
        return result
    result.manifest_applied = True

    # 5. scale.
    if not _kubectl_scale(worker, replicas, namespace=namespace):
        result.error = f"manifest aplicado mas scale de {worker} falhou"
        return result
    result.scaled = True
    result.ok = True
    return result


def uninstall_cli_worker(
    kind: str, *, namespace: str = "deile",
) -> CliWorkerInstallResult:
    """Remove um CLI worker do cluster (idempotente).

    Deleta Deployment, Service, NetworkPolicy e o bearer Secret do worker. NÃO
    toca no Secret compartilhado ``cli-worker-keys`` (outros workers podem usá-lo)
    nem no ConfigMap allowed-repos (compartilhado). Recursos ausentes → no-op.
    """
    worker = f"{kind}-worker"
    resources = [
        ("deployment", worker),
        ("service", worker),
        ("networkpolicy", f"{worker}-netpol"),
        ("secret", f"{worker}-bearer"),
    ]
    failures: List[str] = []
    for res_kind, name in resources:
        try:
            res = subprocess.run(
                ["kubectl", "-n", namespace, "delete", res_kind, name,
                 "--ignore-not-found=true", "--wait=false"],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{res_kind}/{name}: delete timed out")
            continue
        except FileNotFoundError:
            return CliWorkerInstallResult(
                ok=False, kind=kind, error="kubectl não encontrado no PATH",
            )
        if res.returncode != 0:
            failures.append(
                f"{res_kind}/{name}: {res.stderr.strip() or res.stdout.strip()}"
            )
    if failures:
        return CliWorkerInstallResult(
            ok=False, kind=kind, error="; ".join(failures),
        )
    return CliWorkerInstallResult(ok=True, kind=kind)


__all__ = [
    "CliWorkerInstallResult",
    "install_cli_worker",
    "uninstall_cli_worker",
]
