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

* **strategy="exec_inplace"** — ``kubectl exec`` no pod ``claude-worker``
  para ler o ``credentials.json`` que o próprio ``claude -p`` refrescou
  in-pod, depois ``kubectl patch`` no Secret ``claude-credentials`` com
  o valor novo. Não exige browser, não exige operador.

* **strategy="rollout_only"** — apenas ``kubectl rollout restart``. Útil
  quando o token in-pod é o mais recente e o Secret apenas precisa ser
  re-aplicado na próxima inicialização (caso típico do N1: o initContainer
  preserva o PVC porque o Secret é mais velho; rollout não muda nada).

Limitação fundamental: não há renovação cega. Quando o ``refresh_token``
do próprio OAuth expira (limites maiores que o ``accessToken`` — tipo 30
dias), nenhuma das estratégias funciona — só ``claude login`` no host
resolve. Esse caminho permanece manual e é o "estado de bloqueio" final
do Nível 3.

Segurança:

* Toda chamada a ``kubectl`` tem timeout finito (15-60s) para nunca
  pendurar o pipeline.
* O conteúdo do token só é loggado como ``len(...)`` — nunca o valor cru.
* Lock por arquivo (:func:`_acquire_refresh_lock`) impede duas tentativas
  concorrentes (do pipeline e do CronJob simultaneamente).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


#: TTL do lock-file. Refresh real demora <60s; 5min cobre folgadamente.
_REFRESH_LOCK_TTL_S: int = 300

#: Caminho default do lock-file usado para evitar refresh concorrente
#: entre pipeline-implementer e CronJob. Override via env para testes.
_REFRESH_LOCK_PATH: str = os.environ.get(
    "DEILE_CLAUDE_REFRESH_LOCK",
    "/tmp/deile-claude-creds-refresh.lock",
)


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
) -> RefreshResult:
    """Tenta renovar o OAuth do ``claude-worker`` sem operador.

    Args:
        namespace: namespace K8s alvo (default: ``$DEILE_K8S_NAMESPACE`` ou
            ``deile``).
        min_expiry_window_s: se >0, faz refresh apenas se o ``expiresAt``
            atual está a menos de ``min_expiry_window_s`` segundos no futuro.
            ``0.0`` força refresh independente do TTL atual (uso do pipeline
            quando o worker já reportou ``WORKER_AUTH_EXPIRED``).
        skip_lock: se True, ignora o lock-file (uso em testes).

    Returns:
        :class:`RefreshResult` com ``ok=True`` quando o Secret foi atualizado
        com um token válido. ``ok=False`` quando refresh foi pulado (lock,
        TTL ok) ou falhou (refresh_token expirado, kubectl erro). O caller
        usa ``ok`` + ``message`` para decidir entre retry e block.
    """
    ns = namespace or os.environ.get("DEILE_K8S_NAMESPACE", "deile")
    result = RefreshResult(ok=False, strategy="exec_inplace")
    result.steps.append(f"namespace={ns}")

    if not skip_lock and not _acquire_refresh_lock():
        result.message = "outro refresh em andamento (lock ativo)"
        result.steps.append("lock=held")
        return result
    result.steps.append("lock=acquired")

    try:
        creds, msg = await _read_pod_credentials(ns)
        result.steps.append(f"pod_read={msg}")
        if creds is None:
            result.error = f"falha lendo credentials do pod: {msg}"
            return result

        expires_at_ms = _extract_expires_at(creds)
        now_ms = int(time.time() * 1000)
        if expires_at_ms is not None:
            remaining_s = (expires_at_ms - now_ms) / 1000.0
            result.seconds_until_new_expiry = remaining_s
            result.steps.append(
                f"pod_token_expires_in={int(remaining_s)}s"
            )
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
            # Modo reativo: se o token in-pod ainda está válido por >5min,
            # provavelmente o claude refrescou no meio do dispatch. Vale
            # propagar pro Secret e retentar — não é refresh real mas
            # ressincroniza o estado.
            if remaining_s <= 0:
                result.error = (
                    f"token in-pod TAMBÉM expirado "
                    f"(há {int(-remaining_s)}s) — refresh_token provavelmente "
                    f"expirou; humano precisa rodar claude-login --switch"
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
            f"Secret claude-credentials atualizado a partir do token in-pod"
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
