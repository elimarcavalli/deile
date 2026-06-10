"""Helpers de baixo nível para operações kubectl sobre Secrets.

Centraliza o padrão ``kubectl create secret generic ... --dry-run=client -o
yaml | kubectl apply -f -`` (idempotente) e a leitura ``kubectl get secret
-o jsonpath`` + base64-decode, antes duplicados entre
``_cli_worker_install.py`` (Decisão #51) e ``_cli_worker_login.py``.

Timeouts conservadores (15 s dry-run, 30 s apply) impedem que ``kubectl``
pendurado em DNS/auth issue trave o painel/CLI indefinidamente — política
herdada do código original.

Por que mantém ``_claude_install.py`` fora deste módulo: a função
``_kubectl_apply_secret`` lá não passa ``-n`` no ``apply`` (só no dry-run);
o YAML carrega ``metadata.namespace``, então funciona, mas é um caminho
sutilmente diferente que vale a pena cobrir em refator dedicado depois
desse pull-request — ver issue futura.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def apply_generic_secret(
    name: str,
    literals: Dict[str, str],
    *,
    namespace: str,
    dry_timeout_s: int = 15,
    apply_timeout_s: int = 30,
) -> bool:
    """Cria/atualiza um Secret ``generic`` via ``dry-run|apply`` (idempotente).

    Equivalente a::

        kubectl create secret generic <name> \
            --from-literal=K=V ... -n <ns> --dry-run=client -o yaml | \
            kubectl -n <ns> apply -f -

    Valores via ``--from-literal`` no pipe — nunca tocam o disco do host e
    nunca aparecem em logs (``stderr`` é logado em falha mas o YAML do
    dry-run permanece em memória).

    Retorna ``True`` em sucesso, ``False`` em qualquer falha de I/O com
    kubectl ou returncode != 0; loga o erro com contexto antes de retornar.
    """
    if not literals:
        logger.error("apply_generic_secret %s: literals vazio — nada a aplicar", name)
        return False
    literal_args: List[str] = [f"--from-literal={k}={v}" for k, v in literals.items()]
    try:
        dry = subprocess.run(
            ["kubectl", "create", "secret", "generic", name,
             *literal_args, "-n", namespace, "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, check=False, timeout=dry_timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.error("dry-run Secret %s timed out (%ds)", name, dry_timeout_s)
        return False
    except FileNotFoundError:
        logger.error("kubectl não encontrado no PATH")
        return False
    if dry.returncode != 0:
        logger.error("dry-run Secret %s falhou: %s", name, dry.stderr)
        return False
    try:
        apply = subprocess.run(
            ["kubectl", "-n", namespace, "apply", "-f", "-"],
            input=dry.stdout, capture_output=True, text=True,
            check=False, timeout=apply_timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.error("apply Secret %s timed out (%ds)", name, apply_timeout_s)
        return False
    if apply.returncode != 0:
        logger.error("apply Secret %s falhou: %s", name, apply.stderr)
        return False
    return True


def read_secret_value(
    secret_name: str,
    key: str,
    *,
    namespace: str,
    timeout_s: int = 15,
) -> Optional[str]:
    """Lê e decodifica um valor de Secret via ``kubectl get -o jsonpath``.

    Equivalente a::

        kubectl -n <ns> get secret <name> -o jsonpath='{.data.<key>}' | base64 -d

    Retorna o valor decodificado (utf-8) ou ``None`` se: o Secret não existe,
    a chave não existe, base64 inválido ou kubectl falha. Loga em falha real
    (kubectl não-zero), silencioso em "ausente" (chave vazia = caso normal).
    """
    try:
        get = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", secret_name,
             "-o", f"jsonpath={{.data.{key}}}"],
            capture_output=True, text=True, check=False, timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.error("kubectl get secret %s falhou: %s", secret_name, exc)
        return None
    raw = get.stdout.strip()
    if get.returncode != 0 or not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error(
            "Secret %s key %s não é base64 utf-8: %s", secret_name, key, exc,
        )
        return None


def read_secret_data_map(
    secret_name: str,
    *,
    namespace: str,
    timeout_s: int = 15,
) -> Optional[Dict[str, str]]:
    """Lê TODO o ``.data`` de um Secret como ``{key: decoded_utf8_value}``.

    Útil para merge (caso de uso original em ``_cli_worker_install.py``:
    preservar chaves existentes ao instalar um segundo worker). Retorna
    ``None`` se Secret não existe ou kubectl falha; ``{}`` para Secret
    presente mas vazio. Valores em base64 inválido são silenciosamente
    pulados (mesma semântica do código original).
    """
    try:
        get = subprocess.run(
            ["kubectl", "-n", namespace, "get", "secret", secret_name,
             "-o", "jsonpath={.data}"],
            capture_output=True, text=True, check=False, timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if get.returncode != 0 or not get.stdout.strip():
        return None
    try:
        data = _json.loads(get.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    decoded: Dict[str, str] = {}
    for k, b64 in data.items():
        try:
            decoded[k] = base64.b64decode(b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
    return decoded


def sync_bearer_secret(
    *,
    source_secret: str,
    source_key: str,
    target_secret: str,
    target_key: str,
    namespace: str,
) -> bool:
    """Copia ``source_secret[source_key]`` para ``target_secret[target_key]``.

    Padrão da frota: o pipeline envia ``Authorization: Bearer <AUTH_TOKEN do
    worker-bearer>`` para QUALQUER worker (claude/deile/cli-fleet); o init
    container do worker monta o ``<worker>-bearer`` correspondente. Reusar
    o mesmo token mantém todos sob a mesma boundary da NetworkPolicy ingress.

    Retorna ``True`` mesmo se o source não existe (operador precisa rodar
    ``k8s up`` antes — emite warning, mas o rollout do worker ficará
    pending até o source aparecer; não-fatal). ``False`` apenas em erro
    real de I/O com kubectl.
    """
    token = read_secret_value(source_secret, source_key, namespace=namespace)
    if token is None:
        logger.warning(
            "secret %s ausente — rode `deploy.py k8s up` antes (rollout fica "
            "pending até o secret existir)", source_secret,
        )
        return True  # não-fatal
    return apply_generic_secret(
        target_secret, {target_key: token}, namespace=namespace,
    )


__all__ = [
    "apply_generic_secret",
    "read_secret_value",
    "read_secret_data_map",
    "sync_bearer_secret",
]
