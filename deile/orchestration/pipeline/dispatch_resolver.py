"""Dispatch resolver — espelha :mod:`model_resolver` mas para a escolha de
worker (qual pod recebe o POST /v1/dispatch) ao invés de modelo.

Cada stage do pipeline (``classify``, ``refine``, ``implement``, ``pr_review``,
``follow_ups``) pode ter seu dispatcher overriden via env var ou
``~/.deile/settings.json``; sem override, cai pro global
``DEILE_PIPELINE_DISPATCH_MODE`` / ``pipeline.dispatch_mode``; sem isso,
default built-in é ``deile-worker``.

A escolha entre os dois é independente da escolha do modelo (issue #309
correção do user: worker ≠ modelo). ``claude-worker`` só aceita modelos
``anthropic:*``; ``deile-worker`` aceita qualquer modelo.

Precedência de ``resolve_stage_dispatcher`` (alta → baixa):

1. ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env var — vence tudo (retrocompat +
   override emergencial de cluster). Valor inválido → ValueError fail-fast.
2. ``pipeline.dispatchers.<stage>`` no settings.json layered. Valor inválido
   → warning + fallback (erro de usuário, não de operador).
3. ``DEILE_PIPELINE_DISPATCH_MODE`` env var (global). Valor inválido →
   ValueError fail-fast.
4. ``pipeline.dispatch_mode`` no settings.json layered. Valor inválido →
   warning + fallback.
5. Built-in default: ``deile-worker``.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation
from typing import FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

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

#: Built-in timeout defaults (seconds) when no per-stage or global override is set.
#: claude-worker runs ``claude -p`` subprocesses that take longer; deile-worker
#: is in-process and faster. Mirrors ``pipeline_claude_timeout`` (1800) and
#: the new ``pipeline_deile_timeout`` (900) defaults in Settings.
BUILT_IN_TIMEOUT_S_CLAUDE: int = 1800
BUILT_IN_TIMEOUT_S_DEILE: int = 900

#: Built-in max retries default — formerly hard-coded in the monitor loop.
#: Extracted here (issue #391) so it can be overridden per-stage or globally.
BUILT_IN_MAX_RETRIES: int = 3

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


def _canonicalize_settings(value: Optional[str], context: str) -> Optional[str]:
    """Like ``_canonicalize`` but logs a warning instead of raising on invalid.

    Used for settings.json values: a user typo should fall through to the next
    precedence level rather than crashing the pipeline with a ValueError.
    """
    if not value or not value.strip():
        return None
    try:
        return _canonicalize(value)
    except ValueError:
        logger.warning(
            "dispatch_resolver: invalid dispatcher %r in settings.json (%s); "
            "ignoring — expected one of %s",
            value,
            context,
            sorted(VALID_DISPATCHERS),
        )
        return None


def resolve_stage_dispatcher(stage: str) -> str:
    """Resolve qual dispatcher (worker pod) recebe o dispatch de *stage*.

    Fallback chain (top → bottom):

    1. ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env var — fail-fast on invalid.
    2. ``pipeline.dispatchers.<stage>`` in settings.json — warn + skip on invalid.
    3. ``DEILE_PIPELINE_DISPATCH_MODE`` env var — fail-fast on invalid.
    4. ``pipeline.dispatch_mode`` in settings.json — warn + skip on invalid.
    5. Built-in default: ``deile-worker``.

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

    # 1. Env var per-stage (fail-fast: ops config errors must surface loud)
    stage_env = os.environ.get(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}")
    resolved = _canonicalize(stage_env)
    if resolved:
        return resolved

    # 2. Settings per-stage (graceful: user config errors fall through with warning)
    from deile.config.settings import get_settings  # lazy: avoids import cycle
    settings = get_settings()
    settings_per_stage = getattr(settings, f"pipeline_dispatcher_{stage}", None)
    resolved = _canonicalize_settings(
        settings_per_stage, f"pipeline.dispatchers.{stage}"
    )
    if resolved:
        return resolved

    # 3. Env var global (fail-fast)
    global_env = os.environ.get("DEILE_PIPELINE_DISPATCH_MODE")
    resolved = _canonicalize(global_env)
    if resolved:
        return resolved

    # 4. Settings global (graceful); default "deile_worker" canonicalizes to
    #    "deile-worker", so this step also covers the built-in default (step 5).
    resolved = _canonicalize_settings(
        settings.pipeline_dispatch_mode, "pipeline.dispatch_mode"
    )
    if resolved:
        return resolved

    # 5. Hardcoded safety net (only reached if settings.pipeline_dispatch_mode
    #    is empty or an unrecognized alias — extremely unlikely in practice).
    return _DEFAULT_DISPATCHER


def resolve_stage_timeout_s(stage: str) -> int:
    """Returns per-stage dispatch timeout in seconds, falling back to global default.

    Fallback chain (high → low priority):

    1. ``DEILE_PIPELINE_TIMEOUT_S_<STAGE>`` env var — fail-fast on invalid.
    2. ``pipeline.timeouts_s.<stage>`` in settings.json — warn + skip on invalid.
    3. Global settings: ``pipeline_claude_timeout`` (claude-worker) or
       ``pipeline_deile_timeout`` (deile-worker), when set.
    4. Built-in: :data:`BUILT_IN_TIMEOUT_S_CLAUDE` / :data:`BUILT_IN_TIMEOUT_S_DEILE`.

    Raises:
        ValueError: stage not in :data:`PIPELINE_STAGES` (programming bug).
        ValueError: env var contains a non-positive integer (fail-fast).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    # 1. Per-stage env var (fail-fast)
    raw_env = os.environ.get(f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}")
    if raw_env and raw_env.strip():
        try:
            v = int(raw_env.strip())
            if v <= 0:
                raise ValueError(
                    f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()} must be > 0, got {v!r}"
                )
            return v
        except ValueError as exc:
            raise ValueError(
                f"invalid DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}={raw_env!r}: {exc}"
            ) from exc

    # 2. Per-stage settings (graceful)
    from deile.config.settings import get_settings  # lazy: avoids import cycle
    settings = get_settings()
    settings_val = getattr(settings, f"pipeline_timeout_s_{stage}", None)
    if settings_val is not None and settings_val > 0:
        return settings_val

    # 3. Global settings fallback — dispatcher-aware (claude vs deile)
    dispatcher = resolve_stage_dispatcher(stage)
    if dispatcher == "claude-worker":
        global_val = settings.pipeline_claude_timeout
        if global_val is not None and global_val > 0:
            return global_val
        return BUILT_IN_TIMEOUT_S_CLAUDE
    else:
        global_val = settings.pipeline_deile_timeout
        if global_val is not None and global_val > 0:
            return global_val
        return BUILT_IN_TIMEOUT_S_DEILE


def resolve_stage_max_retries(stage: str) -> int:
    """Returns per-stage max retries, falling back to global default.

    Fallback chain (high → low priority):

    1. ``DEILE_PIPELINE_RETRIES_<STAGE>`` env var — fail-fast on invalid.
    2. ``pipeline.retries.<stage>`` in settings.json — warn + skip on invalid.
    3. ``pipeline.default_max_retries`` in settings.json (global default).
    4. Built-in: :data:`BUILT_IN_MAX_RETRIES` (3).

    Raises:
        ValueError: stage not in :data:`PIPELINE_STAGES` (programming bug).
        ValueError: env var contains a negative integer (fail-fast).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    # 1. Per-stage env var (fail-fast)
    raw_env = os.environ.get(f"DEILE_PIPELINE_RETRIES_{stage.upper()}")
    if raw_env is not None and raw_env.strip():
        try:
            v = int(raw_env.strip())
            if v < 0:
                raise ValueError(
                    f"DEILE_PIPELINE_RETRIES_{stage.upper()} must be >= 0, got {v!r}"
                )
            return v
        except ValueError as exc:
            raise ValueError(
                f"invalid DEILE_PIPELINE_RETRIES_{stage.upper()}={raw_env!r}: {exc}"
            ) from exc

    # 2. Per-stage settings (graceful)
    from deile.config.settings import get_settings  # lazy: avoids import cycle
    settings = get_settings()
    settings_val = getattr(settings, f"pipeline_retries_{stage}", None)
    if settings_val is not None:
        return settings_val

    # 3. Global settings default
    if settings.pipeline_default_max_retries is not None:
        return settings.pipeline_default_max_retries

    # 4. Built-in
    return BUILT_IN_MAX_RETRIES


def resolve_stage_cost_cap_usd(stage: str) -> Optional[Decimal]:
    """Return per-stage cost cap in USD, or None if no cap configured.

    Fallback chain (5 levels — mirrors resolve_stage_dispatcher):

    1. ``DEILE_PIPELINE_COST_CAP_USD_<STAGE>`` env var — decimal string, e.g.
       ``"5.00"``.  Invalid value → ValueError fail-fast.
    2. ``pipeline.cost_caps_usd.<stage>`` in settings.json — graceful warn +
       skip on invalid.
    3. ``DEILE_PIPELINE_COST_CAP_USD`` env var (global fallback for all stages).
       Invalid → ValueError fail-fast.
    4. ``pipeline.cost_cap_usd`` in settings.json (global).  Graceful warn.
    5. ``None`` — no cap (current behavior, unlimited).

    Args:
        stage: canonical stage name.

    Returns:
        Positive Decimal in USD, or None when no cap is configured.

    Raises:
        ValueError: stage is not in PIPELINE_STAGES (programming bug).
        ValueError: an env var contains a non-positive or non-parseable value.
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    def _parse_cap(raw: Optional[str], context: str, *, strict: bool) -> Optional[Decimal]:
        if not raw or not raw.strip():
            return None
        stripped = raw.strip()
        try:
            d = Decimal(stripped)
        except InvalidOperation as exc:
            msg = f"invalid decimal {stripped!r} for cost cap ({context})"
            if strict:
                raise ValueError(msg) from exc
            logger.warning("dispatch_resolver: %s — ignoring", msg)
            return None
        if d <= 0:
            msg = f"cost cap must be positive, got {d} ({context})"
            if strict:
                raise ValueError(msg)
            logger.warning("dispatch_resolver: %s — ignoring", msg)
            return None
        return d

    # 1. Per-stage env var (fail-fast on invalid).
    stage_env = os.environ.get(f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}")
    cap = _parse_cap(stage_env, f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}", strict=True)
    if cap is not None:
        return cap

    # 2. Settings per-stage (graceful).
    from deile.config.settings import get_settings  # noqa: PLC0415 — lazy import
    settings = get_settings()
    settings_val = getattr(settings, f"pipeline_cost_cap_usd_{stage}", None)
    if settings_val is not None:
        if isinstance(settings_val, Decimal) and settings_val > 0:
            return settings_val
        # Non-Decimal or non-positive — log and fall through.
        logger.warning(
            "dispatch_resolver: invalid cost cap %r in settings.json "
            "(pipeline.cost_caps_usd.%s) — ignoring",
            settings_val, stage,
        )

    # 3. Global env var fallback (fail-fast on invalid).
    global_env = os.environ.get("DEILE_PIPELINE_COST_CAP_USD")
    cap = _parse_cap(global_env, "DEILE_PIPELINE_COST_CAP_USD", strict=True)
    if cap is not None:
        return cap

    # 4. Global settings (graceful).
    global_settings = getattr(settings, "pipeline_cost_cap_usd", None)
    if global_settings is not None:
        if isinstance(global_settings, Decimal) and global_settings > 0:
            return global_settings

    # 5. No cap.
    return None


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
