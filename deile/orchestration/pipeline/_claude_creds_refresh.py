"""Refresh proativo do OAuth do ``claude-worker`` a partir do cluster.

Este módulo concentra a lógica de "tentar renovar o token OAuth do
``claude-worker`` sem precisar do operador humano". É consumido por duas
frentes:

1. **Pipeline auto-renew (Nível 3)** — ``implementer.py`` invoca
   :func:`try_refresh_claude_credentials` quando o worker retorna
   ``WORKER_AUTH_EXPIRED``. Em caso de sucesso, o pipeline retenta o
   dispatch UMA vez antes de cair no caminho de bloqueio determinístico.

2. **CronJob de renovação proativa (Nível 2)** — o manifest 51 monta um
   CronJob que invoca o mesmo entry-point em modo CLI a cada 4h. Ele
   inspeciona o ``expiresAt`` corrente no pod e, se estiver a menos de 2h
   da expiração, dispara o refresh.

Estratégias suportadas (em ordem de tentativa):

* **strategy="oauth_grant"** — POST real ao endpoint OAuth com
  ``grant_type=refresh_token``. Rotaciona o ``refreshToken``, escreve o
  novo par (``accessToken`` + ``refreshToken`` + ``expiresAt``) de volta
  no PVC (``credentials.json``) **antes** de atualizar o Secret K8s —
  ordem crítica para durabilidade: se a escrita do PVC falhar, o token
  recém-rotacionado não se perde; se só o Secret falhar, o próximo refresh
  pode reconstruí-lo sem um 2º POST.

* **strategy="exec_inplace"** — ``kubectl exec`` no pod ``claude-worker``
  para ler o ``credentials.json`` que o próprio ``claude -p`` refrescou
  in-pod, depois ``kubectl patch`` no Secret ``claude-credentials`` com
  o valor novo. Não exige browser, não exige operador. Usado como
  fallback quando ``refreshToken`` não está disponível.

* **strategy="rollout_only"** — apenas ``kubectl rollout restart``. Útil
  quando o token in-pod é o mais recente e o Secret apenas precisa ser
  re-aplicado na próxima inicialização (caso típico do N1: o initContainer
  preserva o PVC porque o Secret é mais velho; rollout não muda nada).

Limitação fundamental: quando o ``refresh_token`` do OAuth expira
(tipicamente 30 dias), nenhuma estratégia funciona — só ``claude login``
no host resolve. Esse caminho permanece manual e é o "estado de bloqueio"
final do Nível 3.

Segurança:

* Toda chamada a ``kubectl`` tem timeout finito (15-60s) para nunca
  pendurar o pipeline.
* O conteúdo do token só é loggado como ``len(...)`` — nunca o valor cru.
* Lock por arquivo (:func:`_acquire_refresh_lock`) impede duas tentativas
  concorrentes (do pipeline e do CronJob simultaneamente).
* Lock cross-pod via ConfigMap ``claude-credentials-lock`` serializa
  refreshes entre réplicas do ``claude-worker``.
* Endpoint OAuth e ``client_id`` NUNCA são hardcoded — lidos das
  credentials ou de env vars (``CLAUDE_OAUTH_TOKEN_URL``,
  ``CLAUDE_OAUTH_CLIENT_ID``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


#: TTL do lock-file. Refresh real demora <60s; 5min cobre folgadamente.
_REFRESH_LOCK_TTL_S: int = 300

#: Caminho default do lock-file usado para evitar refresh concorrente
#: entre pipeline-implementer e CronJob. Override via env para testes.
_REFRESH_LOCK_PATH: str = os.environ.get(
    "DEILE_CLAUDE_REFRESH_LOCK",
    "/tmp/deile-claude-creds-refresh.lock",
)

#: ConfigMap usado como lock cross-pod para serializar refreshes entre réplicas.
_CROSS_POD_LOCK_CONFIGMAP: str = "claude-credentials-lock"

#: TTL do lock cross-pod em segundos. Alinhado a _REFRESH_LOCK_TTL_S.
_CROSS_POD_LOCK_TTL_S: int = _REFRESH_LOCK_TTL_S

#: Timeout para POST OAuth. AC5: ≤30s.
_OAUTH_POST_TIMEOUT_S: float = 30.0


class InvalidGrantError(Exception):
    """O servidor OAuth retornou ``error=invalid_grant``.

    Indica que o ``refresh_token`` expirou ou foi revogado. A única
    recuperação possível é re-login manual (``claude-login --switch``).
    """


class LockHeldError(Exception):
    """Lock cross-pod ativo e não expirado.

    O caller deve aguardar e retentar; não tenta o grant nesta tentativa.
    """


@dataclass
class RefreshResult:
    """Resultado estruturado de uma tentativa de refresh."""

    ok: bool
    strategy: str = ""
    message: str = ""
    error: str = ""
    # Tempo em segundos até a nova expiração (None se desconhecido).
    seconds_until_new_expiry: Optional[float] = None
    # Steps executados (para debugging/audit).
    steps: List[str] = field(default_factory=list)


def _acquire_refresh_lock(lock_path: Optional[str] = None) -> bool:
    """Adquire lock por arquivo simples.

    Cria ``<lock_path>`` com timestamp se inexistente OU se o existente é
    mais velho que :data:`_REFRESH_LOCK_TTL_S` (lock stale). Retorna True
    quando o caller pode prosseguir.

    Best-effort: erros de I/O caem em ``True`` (fail-open — preferimos
    duas tentativas eventuais a bloquear o pipeline numa I/O glitch).
    """
    path = Path(lock_path or _REFRESH_LOCK_PATH)
    now = time.time()
    try:
        if path.exists():
            age = now - path.stat().st_mtime
            if age < _REFRESH_LOCK_TTL_S:
                return False
        path.write_text(str(int(now)), encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("refresh lock I/O falhou (fail-open): %s", exc)
        return True


def _release_refresh_lock(lock_path: Optional[str] = None) -> None:
    """Remove o lock-file. Idempotente."""
    path = Path(lock_path or _REFRESH_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("refresh lock release falhou: %s", exc)


async def _kubectl_async(
    args: List[str], *, timeout_s: float = 30.0, input_data: Optional[bytes] = None,
) -> tuple[int, str, str]:
    """Executa ``kubectl`` async com timeout. Retorna ``(rc, stdout, stderr)``.

    Em erro de spawn / timeout, retorna ``(-1, "", "<motivo>")`` — nunca
    levanta. O caller decide se é fatal.
    """
    kubectl = os.environ.get("KUBECTL_BIN", "kubectl")
    try:
        proc = await asyncio.create_subprocess_exec(
            kubectl, *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return (-1, "", f"kubectl spawn failed: {exc}")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return (-1, "", f"kubectl timed out after {timeout_s}s")
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", "replace"),
        stderr_b.decode("utf-8", "replace"),
    )


def _extract_expires_at(creds: dict) -> Optional[int]:
    """Extrai ``expiresAt`` (ms epoch) do dict de credentials.

    Aceita o formato Keychain macOS (``claudeAiOauth.expiresAt``) e o
    root-level (``expiresAt`` direto). Retorna ``None`` se ausente/inválido.
    """
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    val = (oauth or {}).get("expiresAt") if isinstance(oauth, dict) else None
    if val is None and isinstance(creds, dict):
        val = creds.get("expiresAt")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


async def _read_pod_credentials(
    namespace: str, *, timeout_s: float = 30.0,
) -> tuple[Optional[dict], str]:
    """Lê ``/home/claude/.claude/credentials.json`` do pod ``claude-worker``.

    Returns ``(creds_dict, message)``. ``creds_dict`` é None se a leitura
    falhou; ``message`` descreve a falha para audit.
    """
    args = [
        "-n", namespace, "exec", "deploy/claude-worker",
        "-c", "claude-worker", "--",
        "cat", "/home/claude/.claude/credentials.json",
    ]
    rc, stdout, stderr = await _kubectl_async(args, timeout_s=timeout_s)
    if rc != 0:
        return None, f"kubectl exec falhou (rc={rc}): {stderr[:200]}"
    try:
        creds = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"credentials.json malformado no pod: {exc}"
    if not isinstance(creds, dict):
        return None, "credentials.json não é um objeto JSON"
    return creds, "ok"


def _extract_oauth_config(creds: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extrai (token_url, client_id, refresh_token) do dict de credentials.

    Ordem de preferência para cada campo:
    1. Valor em ``credentials.json`` (``claudeAiOauth.*``).
    2. Variável de ambiente (``CLAUDE_OAUTH_TOKEN_URL``, ``CLAUDE_OAUTH_CLIENT_ID``).
    3. ``None`` (o caller decide se isso é fatal).

    Nunca retorna URLs ou IDs hardcoded — AC3.
    """
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    oauth = oauth if isinstance(oauth, dict) else {}

    # token_url: campo oauthUrl ou tokenUrl na raiz ou dentro de claudeAiOauth.
    token_url = (
        oauth.get("oauthUrl")
        or oauth.get("tokenUrl")
        or (creds.get("oauthUrl") if isinstance(creds, dict) else None)
        or (creds.get("tokenUrl") if isinstance(creds, dict) else None)
        or os.environ.get("CLAUDE_OAUTH_TOKEN_URL")
    )

    # client_id: campo clientId ou client_id.
    client_id = (
        oauth.get("clientId")
        or oauth.get("client_id")
        or os.environ.get("CLAUDE_OAUTH_CLIENT_ID")
    )

    # refresh_token: campo refreshToken ou refresh_token.
    refresh_token = (
        oauth.get("refreshToken")
        or oauth.get("refresh_token")
    )

    return token_url, client_id, refresh_token


async def _do_oauth_token_refresh(
    token_url: str,
    client_id: str,
    refresh_token: str,
    *,
    timeout_s: float = _OAUTH_POST_TIMEOUT_S,
) -> dict:
    """Realiza o POST OAuth ``grant_type=refresh_token`` e retorna o novo payload.

    Retorna um dict com campos normalizados:
    ``{"accessToken": ..., "refreshToken": ..., "expiresAt": <ms epoch>}``.

    Levanta:
    - :class:`InvalidGrantError` quando o servidor retorna ``error=invalid_grant``
      (token rotacionado expirou/revogado — humano precisa de re-login).
    - ``RuntimeError`` para outros erros HTTP ou de rede.

    Nota: a chamada usa ``asyncio.to_thread`` para não bloquear o event loop
    com I/O síncrono de ``urllib.request``. AC5: POST ≤30s.
    """
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode("utf-8")

    def _sync_post() -> dict:
        req = urllib.request.Request(
            token_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                err_body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                err_body = {}
            err_code = err_body.get("error", "")
            if err_code == "invalid_grant":
                raise InvalidGrantError(
                    f"OAuth refresh_token inválido ou expirado "
                    f"(HTTP {exc.code}): {err_body.get('error_description', raw[:200])}"
                ) from exc
            raise RuntimeError(
                f"OAuth POST falhou (HTTP {exc.code}): "
                f"error={err_code or '?'} body={raw[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"OAuth POST erro de rede: {exc.reason}"
            ) from exc

    data = await asyncio.to_thread(_sync_post)

    # Normaliza campos (o servidor pode usar camelCase ou snake_case).
    access_token = data.get("access_token") or data.get("accessToken")
    new_refresh_token = data.get("refresh_token") or data.get("refreshToken")
    expires_in = data.get("expires_in")
    expires_at_ms = data.get("expiresAt")

    if not access_token:
        raise RuntimeError(
            f"OAuth POST respondeu 200 mas sem access_token/accessToken: {list(data.keys())}"
        )

    # Calcula expiresAt em ms a partir de expires_in (segundos) se não veio direto.
    if expires_at_ms is None and expires_in is not None:
        expires_at_ms = int((time.time() + float(expires_in)) * 1000)

    return {
        "accessToken": access_token,
        "refreshToken": new_refresh_token,
        "expiresAt": expires_at_ms,
    }


async def _acquire_cross_pod_lock(
    namespace: str,
    *,
    pod_name: Optional[str] = None,
    ttl_s: int = _CROSS_POD_LOCK_TTL_S,
    timeout_s: float = 10.0,
) -> bool:
    """Tenta adquirir o lock cross-pod via ConfigMap ``claude-credentials-lock``.

    Cria o ConfigMap se não existe, ou substitui se o lease expirou.
    Retorna ``True`` se o lock foi adquirido, levanta :class:`LockHeldError`
    se outro pod detém o lease dentro do TTL.

    O lock é identificado pelo nome do pod (ou ``hostname`` como fallback).
    TTL auto-expirável: um pod morto mid-refresh não trava a frota.
    """
    holder = pod_name or os.environ.get("POD_NAME") or os.uname().nodename
    expires_ts = str(time.time() + ttl_s)

    # Tenta ler o ConfigMap atual.
    get_args = [
        "-n", namespace, "get", "configmap", _CROSS_POD_LOCK_CONFIGMAP,
        "-o", "json",
    ]
    rc, stdout, _stderr = await _kubectl_async(get_args, timeout_s=timeout_s)

    if rc == 0:
        try:
            existing = json.loads(stdout)
            data = existing.get("data") or {}
            current_holder = data.get("holder", "")
            current_expires = float(data.get("expires", "0"))
            if current_expires > time.time() and current_holder != holder:
                raise LockHeldError(
                    f"lock cross-pod detido por {current_holder!r}, "
                    f"expira em {int(current_expires - time.time())}s"
                )
        except LockHeldError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("parse do ConfigMap de lock falhou (fail-open): %s", exc)

    # Cria ou substitui o ConfigMap com o lease deste pod.
    patch_data = json.dumps({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": _CROSS_POD_LOCK_CONFIGMAP, "namespace": namespace},
        "data": {"holder": holder, "expires": expires_ts},
    })
    apply_args = ["apply", "-f", "-"]
    rc, _out, stderr = await _kubectl_async(
        apply_args, timeout_s=timeout_s,
        input_data=patch_data.encode("utf-8"),
    )
    if rc != 0:
        # Fail-open: se não consegue escrever o lock, prossegue (best-effort).
        logger.warning("falha ao criar ConfigMap de lock (fail-open): %s", stderr[:200])
    return True


async def _release_cross_pod_lock(namespace: str, *, timeout_s: float = 10.0) -> None:
    """Libera o lock cross-pod deletando o ConfigMap."""
    del_args = [
        "-n", namespace, "delete", "configmap", _CROSS_POD_LOCK_CONFIGMAP,
        "--ignore-not-found",
    ]
    rc, _out, stderr = await _kubectl_async(del_args, timeout_s=timeout_s)
    if rc != 0:
        logger.warning("falha ao liberar ConfigMap de lock: %s", stderr[:200])


async def _write_creds_to_pod_pvc(
    namespace: str,
    creds: dict,
    *,
    timeout_s: float = 30.0,
) -> Tuple[bool, str]:
    """Escreve ``credentials.json`` atualizado de volta no PVC do pod.

    Usa ``kubectl exec`` para escrever via stdin no pod. Crítico para AC9:
    o PVC deve ser atualizado ANTES do Secret para que o token rotacionado
    nunca seja perdido. Se esta escrita falhar, o grant já aconteceu mas
    o token rotacionado ainda pode ser recuperado do response em memória —
    o Secret NÃO deve ser atualizado neste caso.

    Retorna ``(ok, message)``.
    """
    creds_json = json.dumps(creds, separators=(",", ":"))
    # Usa `tee` para escrever o stdin no arquivo destino.
    args = [
        "-n", namespace, "exec", "deploy/claude-worker",
        "-c", "claude-worker", "--",
        "sh", "-c",
        "cat > /home/claude/.claude/credentials.json",
    ]
    rc, _out, stderr = await _kubectl_async(
        args, timeout_s=timeout_s,
        input_data=creds_json.encode("utf-8"),
    )
    if rc != 0:
        return False, f"kubectl exec write falhou (rc={rc}): {stderr[:200]}"
    return True, "ok"


async def _patch_secret_with_creds(
    namespace: str, creds: dict, *, timeout_s: float = 30.0,
) -> tuple[bool, str]:
    """Atualiza o Secret ``claude-credentials`` com o conteúdo de ``creds``.

    Usa o pattern padrão ``create --dry-run=client | apply -f -`` para ser
    idempotente. Retorna ``(ok, message)``.
    """
    creds_json = json.dumps(creds)
    dry_args = [
        "create", "secret", "generic", "claude-credentials",
        f"--from-literal=credentials.json={creds_json}",
        "-n", namespace,
        "--dry-run=client", "-o", "yaml",
    ]
    rc, stdout, stderr = await _kubectl_async(dry_args, timeout_s=timeout_s)
    if rc != 0:
        return False, f"kubectl create dry-run falhou: {stderr[:200]}"
    apply_args = ["apply", "-f", "-"]
    rc, _stdout, stderr = await _kubectl_async(
        apply_args, timeout_s=timeout_s, input_data=stdout.encode("utf-8"),
    )
    if rc != 0:
        return False, f"kubectl apply falhou: {stderr[:200]}"
    return True, "ok"


async def try_refresh_claude_credentials(
    *,
    namespace: Optional[str] = None,
    min_expiry_window_s: float = 0.0,
    skip_lock: bool = False,
    skip_cross_pod_lock: bool = False,
) -> RefreshResult:
    """Tenta renovar o OAuth do ``claude-worker`` sem operador.

    Fluxo de decisão:

    1. Lê ``credentials.json`` do pod via ``kubectl exec``.
    2. Verifica ``expiresAt``:
       - ``remaining_s > min_expiry_window_s > 0`` → skip (token OK).
       - ``0 < remaining_s <= min_expiry_window_s`` → grant proativo (AC10).
       - ``remaining_s <= 0`` → grant reativo (AC1).
       - ``expiresAt`` ausente → tenta grant se ``refreshToken`` disponível.
    3. Se ``claudeAiOauth.refreshToken`` está presente:
       a. Adquire lock cross-pod (ConfigMap ``claude-credentials-lock``) (AC4).
       b. Faz POST OAuth ``grant_type=refresh_token`` (AC1/AC10).
       c. Escreve novo token no PVC **antes** do Secret (AC9).
       d. Atualiza o Secret K8s.
    4. Se ``refreshToken`` ausente → fallback para re-sync simples
       (propaga token válido in-pod pro Secret).

    Args:
        namespace: namespace K8s alvo (default: ``$DEILE_K8S_NAMESPACE`` ou
            ``deile``).
        min_expiry_window_s: se >0, faz refresh proativo apenas se o token
            expira em menos de ``min_expiry_window_s`` segundos. ``0.0``
            força refresh independente do TTL atual.
        skip_lock: se True, ignora o lock-file per-pod (uso em testes).
        skip_cross_pod_lock: se True, ignora o lock cross-pod ConfigMap
            (uso em testes ou ambientes sem kubectl com RBAC de ConfigMap).

    Returns:
        :class:`RefreshResult` com ``ok=True`` quando o Secret foi atualizado
        com um token válido. ``ok=False`` quando refresh foi pulado (lock,
        TTL ok) ou falhou (invalid_grant, kubectl erro). O caller usa
        ``ok`` + ``message`` para decidir entre retry e block.
    """
    ns = namespace or os.environ.get("DEILE_K8S_NAMESPACE", "deile")
    result = RefreshResult(ok=False, strategy="exec_inplace")
    result.steps.append(f"namespace={ns}")

    if not skip_lock and not _acquire_refresh_lock():
        result.message = "outro refresh em andamento (lock ativo)"
        result.steps.append("lock=held")
        return result
    result.steps.append("lock=acquired")

    cross_pod_lock_acquired = False
    try:
        creds, msg = await _read_pod_credentials(ns)
        result.steps.append(f"pod_read={msg}")
        if creds is None:
            result.error = f"falha lendo credentials do pod: {msg}"
            return result

        expires_at_ms = _extract_expires_at(creds)
        now_ms = int(time.time() * 1000)
        remaining_s: Optional[float] = None
        if expires_at_ms is not None:
            remaining_s = (expires_at_ms - now_ms) / 1000.0
            result.seconds_until_new_expiry = remaining_s
            result.steps.append(f"pod_token_expires_in={int(remaining_s)}s")

            # Modo proativo (CronJob): só age se está próximo de expirar.
            # Modo reativo (pipeline): min_expiry_window_s=0 sempre prossegue.
            if min_expiry_window_s > 0 and remaining_s > min_expiry_window_s:
                result.message = (
                    f"token ainda válido por {int(remaining_s)}s "
                    f"(threshold={int(min_expiry_window_s)}s) — skip"
                )
                result.ok = True
                result.steps.append("decision=skip-not-near-expiry")
                return result

        # Verifica se temos refreshToken disponível para o grant real.
        token_url, client_id, refresh_token = _extract_oauth_config(creds)

        if refresh_token:
            # Caminho principal: grant real (AC1 + AC10).
            result.strategy = "oauth_grant"
            result.steps.append("strategy=oauth_grant")

            if not token_url:
                result.error = (
                    "refreshToken presente mas token_url ausente — "
                    "defina CLAUDE_OAUTH_TOKEN_URL ou adicione oauthUrl/tokenUrl "
                    "em credentials.json"
                )
                result.steps.append("missing=token_url")
                return result

            if not client_id:
                result.error = (
                    "refreshToken presente mas client_id ausente — "
                    "defina CLAUDE_OAUTH_CLIENT_ID ou adicione clientId "
                    "em claudeAiOauth"
                )
                result.steps.append("missing=client_id")
                return result

            # Adquire lock cross-pod antes do POST (AC4).
            if not skip_cross_pod_lock:
                try:
                    await _acquire_cross_pod_lock(ns)
                    cross_pod_lock_acquired = True
                    result.steps.append("cross_pod_lock=acquired")
                except LockHeldError as exc:
                    result.error = str(exc)
                    result.steps.append("cross_pod_lock=held")
                    return result

            # POST OAuth — ≤30s (AC5).
            try:
                new_tokens = await _do_oauth_token_refresh(
                    token_url, client_id, refresh_token,
                    timeout_s=_OAUTH_POST_TIMEOUT_S,
                )
                result.steps.append(
                    f"oauth_post=ok(accessToken_len={len(new_tokens['accessToken'])},"
                    f"has_refresh={bool(new_tokens.get('refreshToken'))})"
                )
            except InvalidGrantError as exc:
                result.error = (
                    f"OAuth invalid_grant — refresh_token expirou/revogado; "
                    f"humano precisa rodar claude-login --switch. Detalhe: {exc}"
                )
                result.steps.append("oauth_post=invalid_grant")
                return result
            except RuntimeError as exc:
                result.error = f"OAuth POST falhou: {exc}"
                result.steps.append("oauth_post=error")
                return result

            # Constrói credentials atualizadas mesclando novos campos (AC9).
            oauth_block = dict(creds.get("claudeAiOauth") or {})
            oauth_block["accessToken"] = new_tokens["accessToken"]
            if new_tokens.get("refreshToken"):
                oauth_block["refreshToken"] = new_tokens["refreshToken"]
            if new_tokens.get("expiresAt") is not None:
                oauth_block["expiresAt"] = new_tokens["expiresAt"]
            new_creds = dict(creds)
            new_creds["claudeAiOauth"] = oauth_block

            # AC9: escreve PVC ANTES do Secret. Se PVC falhar → Secret intocado.
            ok_pvc, msg_pvc = await _write_creds_to_pod_pvc(ns, new_creds)
            result.steps.append(f"pvc_write={msg_pvc}")
            if not ok_pvc:
                result.error = (
                    f"POST OAuth OK mas escrita no PVC falhou — token rotacionado "
                    f"pode estar perdido; humano deve re-logar. Detalhe: {msg_pvc}"
                )
                result.steps.append("decision=pvc-write-failed-secret-untouched")
                return result

            # Agora atualiza o Secret K8s com os novos tokens.
            ok_secret, msg_secret = await _patch_secret_with_creds(ns, new_creds)
            result.steps.append(f"secret_patch={msg_secret}")
            if not ok_secret:
                result.error = f"PVC atualizado mas Secret K8s falhou: {msg_secret}"
                return result

            if new_tokens.get("expiresAt") is not None:
                new_expiry_s = (new_tokens["expiresAt"] - int(time.time() * 1000)) / 1000.0
                result.seconds_until_new_expiry = new_expiry_s

            result.ok = True
            result.message = (
                "OAuth grant_type=refresh_token OK; Secret e PVC atualizados"
                + (
                    f" (novo token expira em {int(result.seconds_until_new_expiry)}s)"
                    if result.seconds_until_new_expiry is not None
                    else ""
                )
            )
            return result

        # Fallback: sem refreshToken — re-sync simples (comportamento legado).
        result.strategy = "exec_inplace"
        result.steps.append("strategy=exec_inplace(no-refresh-token)")

        if remaining_s is not None and remaining_s <= 0:
            result.error = (
                f"token in-pod TAMBÉM expirado "
                f"(há {int(-remaining_s)}s) — refreshToken ausente; "
                f"humano precisa rodar claude-login --switch"
            )
            result.steps.append("decision=refresh-token-also-expired")
            return result

        ok, msg = await _patch_secret_with_creds(ns, creds)
        result.steps.append(f"secret_patch={msg}")
        if not ok:
            result.error = f"falha aplicando Secret: {msg}"
            return result

        result.ok = True
        result.message = (
            "Secret claude-credentials atualizado a partir do token in-pod (re-sync)"
            + (
                f" (expira em {int(result.seconds_until_new_expiry)}s)"
                if result.seconds_until_new_expiry is not None
                else ""
            )
        )
        return result

    except Exception as exc:  # noqa: BLE001 — never bubble up from this helper
        logger.exception("try_refresh_claude_credentials raised")
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if cross_pod_lock_acquired and not skip_cross_pod_lock:
            await _release_cross_pod_lock(ns)
        if not skip_lock:
            _release_refresh_lock()


# --------------------------------------------------------------------------- #
# CLI entry-point para CronJob (Nível 2)
# --------------------------------------------------------------------------- #


async def _cli_main(argv: Optional[List[str]] = None) -> int:
    """Entry-point invocado pelo CronJob (manifest 51).

    Argumentos via env:
      * ``DEILE_K8S_NAMESPACE`` — namespace alvo (default: ``deile``).
      * ``DEILE_CLAUDE_RENEW_MIN_WINDOW_S`` — janela proativa em segundos
        (default: ``7200`` = 2h; refresh só ocorre se o token expira em
        menos disso).
    """
    del argv  # CLI sem flags próprias — toda config via env.
    logging.basicConfig(
        level=os.environ.get("DEILE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    namespace = os.environ.get("DEILE_K8S_NAMESPACE", "deile")
    window_s = float(os.environ.get("DEILE_CLAUDE_RENEW_MIN_WINDOW_S", "7200"))
    logger.info(
        "claude-creds-refresh cron: namespace=%s min_window_s=%s",
        namespace, window_s,
    )
    result = await try_refresh_claude_credentials(
        namespace=namespace, min_expiry_window_s=window_s,
    )
    for step in result.steps:
        logger.info("step: %s", step)
    if result.ok:
        logger.info("OK: %s", result.message or "refresh concluído")
        return 0
    logger.error("FAIL: %s", result.error or result.message or "refresh falhou")
    return 1


def main() -> int:
    """Sync wrapper para o entry-point CLI (consumido pelo CronJob)."""
    return asyncio.run(_cli_main())


if __name__ == "__main__":
    import sys
    sys.exit(main())
