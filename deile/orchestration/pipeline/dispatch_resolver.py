"""Dispatch resolver — espelha :mod:`model_resolver` mas para a escolha de
worker (qual pod recebe o POST /v1/dispatch) ao invés de modelo.

Cada stage do pipeline (``classify``, ``refine``, ``implement``, ``pr_review``,
``follow_ups``) pode ter seu dispatcher overriden via env var; sem override,
cai pro global ``DEILE_PIPELINE_DISPATCH_MODE``; sem isso, default built-in
é ``deile-worker``.

A escolha entre os dois é independente da escolha do modelo (issue #309
correção do user: worker ≠ modelo). ``claude-worker`` só aceita modelos
``anthropic:*``; ``deile-worker`` aceita qualquer modelo.
"""
from __future__ import annotations

import os
from typing import FrozenSet, Optional, Tuple

#: Ordem operacional (igual model_resolver). Sufixo usado como JSON key.
PIPELINE_STAGES: Tuple[str, ...] = (
    "classify",
    "refine",
    "implement",
    "pr_review",
    "follow_ups",
)

#: Valores aceitos. Frozen para evitar mutação acidental.
VALID_DISPATCHERS: FrozenSet[str] = frozenset({"deile-worker", "claude-worker"})

#: Aliases legacy de PR #330 que canonicalizam para os 2 valores válidos.
#: Necessário para compat com deployments existentes que tenham
#: ``DEILE_PIPELINE_DISPATCH_MODE`` no formato underscore ou abreviado.
#: Mantém em paridade com ``WORKER_ALIASES`` / ``CLAUDE_ALIASES`` de
#: :mod:`deile.orchestration.pipeline.implementer`.
_DISPATCHER_ALIASES: dict[str, str] = {
    "deile_worker": "deile-worker",
    "worker": "deile-worker",
    "deile": "deile-worker",
    "deile-worker": "deile-worker",
    "claude": "claude-worker",
    "claude_code": "claude-worker",
    "claude-code": "claude-worker",
    "claude-worker": "claude-worker",
}

_DEFAULT_DISPATCHER = "deile-worker"

# Default endpoints. Env vars sobrescrevem (útil pra dev local fora do cluster).
_ENDPOINT_DEFAULTS = {
    "deile-worker": "http://deile-worker:8766",
    "claude-worker": "http://claude-worker:8767",
}
_ENDPOINT_ENV_VARS = {
    "deile-worker": "DEILE_WORKER_ENDPOINT",
    "claude-worker": "DEILE_CLAUDE_WORKER_ENDPOINT",
}


def is_valid_dispatcher(value: Optional[str]) -> bool:
    """Returns True se *value* é dispatcher válido (canônico OU legacy alias).

    Case-insensitive; whitespace stripped. Falsy / não-string → False.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISPATCHER_ALIASES


def _canonicalize(value: Optional[str]) -> Optional[str]:
    """Normaliza para forma canônica em VALID_DISPATCHERS; None se vazio.

    Aceita aliases legacy (PR #330) e canonicaliza para os 2 valores válidos.
    Valor não reconhecido → ValueError fail-fast (típico typo).
    """
    if not value or not value.strip():
        return None
    normalized = value.strip().lower()
    canonical = _DISPATCHER_ALIASES.get(normalized)
    if canonical is None:
        raise ValueError(
            f"unknown dispatcher {value!r}; expected one of "
            f"{sorted(VALID_DISPATCHERS)} (or aliases "
            f"{sorted(set(_DISPATCHER_ALIASES) - VALID_DISPATCHERS)})"
        )
    return canonical


def resolve_stage_dispatcher(stage: str) -> str:
    """Resolve qual dispatcher (worker pod) recebe o dispatch de *stage*.

    Fallback chain (top → bottom):
      1. ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env var
      2. ``DEILE_PIPELINE_DISPATCH_MODE`` env var (global default)
      3. Built-in default: ``deile-worker``

    Raises:
        ValueError: stage não está em :data:`PIPELINE_STAGES` (programming bug,
            não user input — implementer methods passam de uma whitelist).
        ValueError: env var contém valor não-whitelisted (fail-fast para evitar
            queimar budget no engine errado por typo).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    stage_env = os.environ.get(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}")
    resolved = _canonicalize(stage_env)
    if resolved:
        return resolved

    global_env = os.environ.get("DEILE_PIPELINE_DISPATCH_MODE")
    resolved = _canonicalize(global_env)
    if resolved:
        return resolved

    return _DEFAULT_DISPATCHER


def get_endpoint_for(dispatcher: str) -> str:
    """Resolve a URL HTTP do worker pod *dispatcher*.

    Env var (``DEILE_WORKER_ENDPOINT`` ou ``DEILE_CLAUDE_WORKER_ENDPOINT``)
    sobrescreve o default — útil para dev local que aponta para localhost
    em vez do Service DNS do cluster.

    Raises:
        ValueError: dispatcher fora de :data:`VALID_DISPATCHERS`.
    """
    canonical = _canonicalize(dispatcher)
    if canonical is None:
        raise ValueError(f"unknown dispatcher {dispatcher!r}")
    env_var = _ENDPOINT_ENV_VARS[canonical]
    return os.environ.get(env_var) or _ENDPOINT_DEFAULTS[canonical]
