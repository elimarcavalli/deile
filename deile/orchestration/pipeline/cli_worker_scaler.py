"""cli_worker_scaler — ensure-replica on-demand dos CLI workers (plano §1.13/B5).

Workers CLI nascem ``replicas: 0`` (scale-to-zero). Quando o pipeline despacha
para um deles, precisa garantir ≥1 réplica ANTES do dispatch — do contrário o
``POST /v1/dispatch`` falha com ``connection refused`` genérico.

Fluxo: lê réplicas via ``kubectl get``; se ≥1 → ``READY``; se 0 → escala com
cooldown anti-flapping → ``SCALED``. Sem kubectl/RBAC → ``NO_KUBECTL``/
``SCALE_FAILED``, que o caller traduz em erro instrutivo ``WORKER_SCALED_TO_ZERO``.

Só atua em dispatchers CLI (não em ``deile-worker``/``claude-worker``, que nascem
com 1 réplica). Reusa ``KUBECTL_BIN``/``DEILE_K8S_NAMESPACE`` da mesma SA do
pipeline pod. Best-effort: nunca levanta.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from deile.orchestration.pipeline.dispatch_resolver import BUILTIN_DISPATCHERS

logger = logging.getLogger(__name__)

#: Cooldown (s) entre scales do mesmo alvo — evita flapping no cold-start. Override por env.
_SCALE_COOLDOWN_S: float = float(
    os.environ.get("DEILE_CLI_WORKER_SCALE_COOLDOWN_S", "120")
)

#: Timeouts das chamadas kubectl — curtos para não pendurar o tick.
_GET_TIMEOUT_S: float = 15.0
_SCALE_TIMEOUT_S: float = 20.0

_last_scale_at: Dict[str, float] = {}  # monotonic ts do último scale por alvo


class ScaleResult(Enum):
    """Resultado do ensure-replica de um dispatcher CLI."""

    READY = "ready"            # já tinha ≥1 réplica; nada a fazer
    SCALED = "scaled"          # estava em 0 → escalado para 1
    COOLDOWN = "cooldown"      # estava em 0 mas escalamos há pouco → aguarda
    SCALE_FAILED = "scale_failed"  # kubectl scale falhou (RBAC/erro)
    NO_KUBECTL = "no_kubectl"  # kubectl ausente no PATH
    NOT_APPLICABLE = "not_applicable"  # dispatcher núcleo — não escalável aqui


@dataclass(frozen=True)
class EnsureReplicaOutcome:
    """Veredito do ensure-replica + detalhe legível para log/erro."""

    result: ScaleResult
    detail: str = ""

    @property
    def ok_to_dispatch(self) -> bool:
        """True se o dispatch pode prosseguir (READY/SCALED/COOLDOWN/NOT_APPLICABLE).

        SCALED/COOLDOWN: há (ou logo haverá) um pod subindo; o readinessProbe segura o
        Service. Falhas (SCALE_FAILED/NO_KUBECTL) bloqueiam.
        """
        return self.result in (
            ScaleResult.READY, ScaleResult.SCALED, ScaleResult.COOLDOWN,
            ScaleResult.NOT_APPLICABLE,
        )


def _kubectl_bin() -> Optional[str]:
    import shutil

    explicit = os.environ.get("KUBECTL_BIN", "").strip()
    if explicit:
        return explicit if (shutil.which(explicit) or os.path.isfile(explicit)) else None
    return shutil.which("kubectl")


def _namespace() -> str:
    return os.environ.get("DEILE_K8S_NAMESPACE", "").strip() or "deile"


async def _kubectl(*args: str, timeout_s: float) -> tuple:
    """Roda ``kubectl <args>`` async; ``(rc, stdout, stderr)``. Nunca levanta."""
    binary = _kubectl_bin()
    if not binary:
        return -1, "", "kubectl not found in PATH"
    ns = _namespace()
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-n", ns, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return -1, "", f"kubectl spawn failed: {exc}"
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"kubectl timed out after {timeout_s}s"
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", "replace"),
        err_b.decode("utf-8", "replace"),
    )


def _deployment_for(dispatcher: str) -> str:
    """Nome do Deployment do dispatcher CLI (``opencode-worker`` → mesmo nome)."""
    return dispatcher


async def ensure_replica(dispatcher: str) -> EnsureReplicaOutcome:
    """Garante ≥1 réplica do Deployment do *dispatcher* CLI antes do dispatch.

    Dispatchers núcleo (``deile-worker``/``claude-worker``) retornam ``NOT_APPLICABLE``.
    """
    if not dispatcher or dispatcher in BUILTIN_DISPATCHERS:
        return EnsureReplicaOutcome(ScaleResult.NOT_APPLICABLE)

    if _kubectl_bin() is None:
        return EnsureReplicaOutcome(
            ScaleResult.NO_KUBECTL,
            f"kubectl indisponível no pipeline pod — escale manualmente: "
            f"`k8s scale --{dispatcher.replace('-worker', '')}-worker 1`",
        )

    deploy = _deployment_for(dispatcher)
    rc, out, err = await _kubectl(
        "get", "deployment", deploy,
        "-o", "jsonpath={.spec.replicas}",
        timeout_s=_GET_TIMEOUT_S,
    )
    if rc != 0:
        return EnsureReplicaOutcome(
            ScaleResult.SCALE_FAILED,
            f"kubectl get deployment/{deploy} falhou (rc={rc}): {err.strip()[:200]}",
        )
    try:
        replicas = int((out or "0").strip() or "0")
    except ValueError:
        replicas = 0
    if replicas >= 1:
        return EnsureReplicaOutcome(ScaleResult.READY, f"{deploy} já com {replicas} réplica(s)")

    now = time.monotonic()
    last = _last_scale_at.get(deploy, 0.0)
    if (now - last) < _SCALE_COOLDOWN_S:
        return EnsureReplicaOutcome(
            ScaleResult.COOLDOWN,
            f"{deploy} escalado há {int(now - last)}s (< cooldown {int(_SCALE_COOLDOWN_S)}s); "
            "pod ainda subindo — dispatch reconcilia no próximo tick",
        )

    rc, _out, err = await _kubectl(
        "scale", f"deployment/{deploy}", "--replicas=1",
        timeout_s=_SCALE_TIMEOUT_S,
    )
    if rc != 0:
        return EnsureReplicaOutcome(
            ScaleResult.SCALE_FAILED,
            f"kubectl scale deployment/{deploy} --replicas=1 falhou (rc={rc}): "
            f"{err.strip()[:200]} — escale manualmente: "
            f"`k8s scale --{deploy.replace('-worker', '')}-worker 1`",
        )
    _last_scale_at[deploy] = now
    logger.info("ensure_replica: %s escalado 0→1 (on-demand)", deploy)
    return EnsureReplicaOutcome(ScaleResult.SCALED, f"{deploy} escalado 0→1")


def _reset_cooldown_for_tests() -> None:
    """Limpa o estado de cooldown (uso só em testes)."""
    _last_scale_at.clear()
