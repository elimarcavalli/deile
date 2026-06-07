"""cli_worker_scaler — ensure-replica on-demand dos CLI workers (plano B5).

Os workers da frota CLI nascem ``replicas: 0`` (scale-to-zero, custo zero
ocioso). Quando o operador roteia um stage para um deles
(``DEILE_PIPELINE_DISPATCH_<STAGE>=opencode-worker``), o pipeline precisa
**garantir ≥1 réplica** ANTES de despachar — senão o ``POST /v1/dispatch`` bate
num Service sem pods e falha com ``connection refused`` genérico, sem auto-scale
e sem mensagem instrutiva.

Comportamento (plano §1.13/B5 — "default: auto-scale 1 com cooldown, reusando o
RBAC do pipeline"):

1. Resolve o Deployment alvo do dispatcher (``<kind>-worker``).
2. Lê as réplicas desejadas via ``kubectl get deploy -o jsonpath``.
3. Se já ≥1 → ``READY`` (nada a fazer).
4. Se 0 → ``kubectl scale --replicas=1`` (com **cooldown** in-memory por alvo
   para não flapar entre ticks) → ``SCALED``.
5. Sem ``kubectl`` no PATH ou sem permissão/erro → devolve ``SCALE_FAILED`` /
   ``NO_KUBECTL`` para o caller transformar num erro tipado
   ``WORKER_SCALED_TO_ZERO`` instruindo ``k8s scale --<kind>-worker 1``.

Só atua sobre dispatchers da frota CLI (``<kind>-worker`` que NÃO são núcleo);
os workers núcleo (``deile-worker``/``claude-worker``) nascem com 1 réplica e
nunca passam por aqui. Reusa ``KUBECTL_BIN``/``DEILE_K8S_NAMESPACE`` como o
``_claude_creds_refresh`` (mesma SA do pipeline pod). Best-effort: nunca levanta
— qualquer falha vira um resultado que o caller traduz em erro instrutivo.
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

#: Cooldown (s) entre scales do mesmo alvo — evita flapping quando vários ticks
#: pegam o worker ainda subindo (cold-start de imagem). Override por env.
_SCALE_COOLDOWN_S: float = float(
    os.environ.get("DEILE_CLI_WORKER_SCALE_COOLDOWN_S", "120")
)

#: Timeouts das chamadas kubectl (curtos — nunca penduram o tick).
_GET_TIMEOUT_S: float = 15.0
_SCALE_TIMEOUT_S: float = 20.0

#: Último instante (monotonic) em que cada alvo foi escalado — anti-flapping.
_last_scale_at: Dict[str, float] = {}


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
        """True quando o dispatch pode prosseguir (ready ou recém-escalado).

        ``SCALED``/``COOLDOWN`` significam que há (ou logo haverá) um pod subindo;
        o dispatch fire-and-forget + reconcile do próximo tick cobre o cold-start
        (o readinessProbe segura o Service até o pod estar pronto). ``READY`` é o
        caminho quente. Falhas (``SCALE_FAILED``/``NO_KUBECTL``) bloqueiam.
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

    Args:
        dispatcher: dispatcher canônico (ex.: ``opencode-worker``). Dispatchers
            núcleo (``deile-worker``/``claude-worker``) → ``NOT_APPLICABLE``.

    Returns:
        :class:`EnsureReplicaOutcome` — ``ok_to_dispatch`` indica se o caller
        pode seguir com o ``POST /v1/dispatch`` ou deve devolver erro instrutivo.
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

    # 0 réplicas → escalar, respeitando o cooldown anti-flapping.
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
