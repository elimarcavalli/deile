"""Conversores/coerção de tipos para settings.py (issue #751).

Módulo folha — sem imports top-level de deile além dos lazy dentro das
funções. As duas dependências internas (``is_valid_dispatcher``,
``is_valid_effort``) são importadas de forma lazy para evitar ciclos:
  settings → pipeline.dispatch_resolver → settings
  settings → core.models.reasoning      (não importa deile, mas lazy por simetria)

Re-exportado integralmente por ``deile.config.settings`` via shim, de modo
que nenhum importador externo precisa mudar.
"""

import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional


def _to_bool(value: Any) -> bool:
    """Strict bool coercion — rejects ambiguous string literals."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.lower().strip()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Cannot coerce {value!r} to bool")
    raise TypeError(f"Expected bool, got {type(value).__name__}")


def _to_str_list(value: Any) -> list:
    """Strict list coercion — rejects non-list values."""
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"Expected list, got {type(value).__name__}")


def _to_optional_path(value: Any) -> Optional[Path]:
    """Convert value to Path, hardened against null bytes and oversized values."""
    if value is None or (isinstance(value, str) and not value):
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"path setting must be string, got {type(value).__name__}")
    s = str(value)
    if "\x00" in s:
        raise ValueError("path setting contains null byte")
    if len(s) > 4096:
        raise ValueError(f"path setting exceeds 4096 chars (got {len(s)})")
    return Path(s).expanduser()


def _mb_to_bytes(value: Any) -> int:
    return int(value) * 1024 * 1024


def _to_optional_positive_int(value: Any) -> Optional[int]:
    """Coerce to a positive int (> 0), or None if absent/empty.

    Used for per-stage timeout_s overrides: None = no override (fall
    through to global/built-in); 0 or negative = rejected.
    Empty string is treated as absent (returns None) for env-var ergonomics.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("expected int, got bool")
    if isinstance(value, str) and not value.strip():
        return None
    iv = int(value)
    if iv <= 0:
        raise ValueError(f"value must be > 0, got {iv}")
    return iv


# Alias kept for internal compatibility (issue #391 spec used this name).
_to_optional_pos_int = _to_optional_positive_int


def _to_optional_nonneg_int(value: Any) -> Optional[int]:
    """Coerce to a non-negative int (>= 0), or None if absent/empty.

    Used for per-stage retries overrides: None = no override; negative = rejected.
    0 is valid (means no retries).
    Empty string is treated as absent (returns None) for env-var ergonomics.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("expected int, got bool")
    if isinstance(value, str) and not value.strip():
        return None
    iv = int(value)
    if iv < 0:
        raise ValueError(f"value must be >= 0, got {iv}")
    return iv


def _to_nonneg_int(value: Any) -> int:
    """Coerce to a non-negative int (rejects negatives and bools).

    Used by the pipeline resume knobs (interval/max_attempts/budget) where 0 is
    a meaningful value (``interval=0`` = immediate, ``budget=0`` = no ceiling)
    but a negative would be nonsensical.
    """
    if isinstance(value, bool):
        raise TypeError("expected int, got bool")
    iv = int(value)
    if iv < 0:
        raise ValueError(f"value must be >= 0, got {iv}")
    return iv


def _to_pos_int(value: Any) -> int:
    """Coerce to a positive int (>= 1; rejects 0/negatives and bools).

    Used by ``resume_max_attempts``: a 0 or negative ceiling would make
    ``attempt >= max_attempts`` true on the first check and block every resume
    instantly. A rejected value is caught by ``apply_overrides`` and leaves the
    default (10) in place.
    """
    if isinstance(value, bool):
        raise TypeError("expected int, got bool")
    iv = int(value)
    if iv < 1:
        raise ValueError(f"value must be >= 1, got {iv}")
    return iv


def _to_pos_int_or_auto(value: Any) -> Any:
    """Coerce to a positive int or the sentinel string ``"auto"``.

    Used by ``DEILE_PIPELINE_MAX_PARALLEL`` and ``pipeline.max_parallel``:
    a numeric string → int ≥ 1; the literal ``"auto"`` → kept as ``"auto"``
    so the pipeline can derive ``max_parallel`` from ``claude-worker``
    replica count at startup instead of a hardcoded ceiling.
    Any other value raises so ``apply_overrides`` logs a warning and keeps
    the previous value (int 2 default).
    """
    if isinstance(value, bool):
        raise TypeError("expected int or 'auto', got bool")
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    iv = int(value)
    if iv < 1:
        raise ValueError(f"value must be >= 1, got {iv}")
    return iv


# Per-stage pipeline model slug (issue #305): ``provider:model``. Mirrors
# `_MODEL_SLUG_RE` in `deile/infrastructure/deile_worker_client.py` — keep in
# sync (both validate the same wire/JSON format).
#
# The ``/`` in the model side is REQUIRED for OpenRouter (gateway), whose model
# ids carry the upstream vendor: ``openrouter:anthropic/claude-sonnet-4.6``.
# Without it, ``_to_optional_model_slug`` would silently drop the override.
_MODEL_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9._/-]+$")


def _to_optional_model_slug(value: Any) -> Optional[str]:
    """Strict converter for ``pipeline.models.<stage>`` entries (issue #305).

    ``None`` and empty/whitespace string collapse to ``None`` (no override).
    Non-strings, or strings that don't match ``provider:model``, raise —
    ``apply_overrides`` catches the exception and keeps the previous (default)
    value. Strict by design: a typo here would silently route every dispatch
    to a non-existent model, manifesting only as a worker-side 5xx many
    minutes later.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        return None
    if not _MODEL_SLUG_RE.match(stripped):
        raise ValueError(
            f"invalid model slug {stripped!r}; expected 'provider:model'"
        )
    return stripped


def _to_optional_dispatcher(value: Any) -> Optional[str]:
    """Strict converter for ``pipeline.dispatchers.<stage>`` entries (issue #309).

    Espelha ``_to_optional_model_slug``: ``None`` / vazio colapsa para ``None``
    (sem override); non-string ou valor fora do whitelist do
    :func:`is_valid_dispatcher` levanta — ``apply_overrides`` engole e mantém
    o valor anterior. Strict by design: um typo aqui rotearia silenciosamente
    todo dispatch para o engine errado (claude-worker dispara billing de
    subscription/API anthropic, deile-worker usa o provider configurado).

    Note: o validator aceita aliases legacy de PR #330 (``deile_worker``,
    ``claude_code``, etc.); a canonicalização para ``deile-worker`` /
    ``claude-worker`` é responsabilidade do :mod:`dispatch_resolver` no
    momento da resolução, não da camada de persistência.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        return None
    # Lazy import — evita import cycle settings → pipeline.dispatch_resolver →
    # settings (resolver consome ``get_settings()`` em runtime).
    from deile.orchestration.pipeline.dispatch_resolver import \
        is_valid_dispatcher
    if not is_valid_dispatcher(stripped):
        raise ValueError(
            f"invalid dispatcher {stripped!r}; expected one of "
            "'deile-worker'/'claude-worker' (or legacy aliases "
            "'deile_worker', 'claude_code', 'worker', 'claude', etc)"
        )
    return stripped


def _to_optional_reasoning_effort(value: Any) -> Optional[str]:
    """Strict converter para reasoning effort (global + por etapa).

    Espelha ``_to_optional_dispatcher``: ``None``/vazio colapsa para ``None``;
    non-string levanta ``TypeError``; token desconhecido levanta ``ValueError``
    (``apply_overrides`` engole e mantém o valor anterior). O conjunto válido
    *final* depende do worker/provider da etapa; aqui validamos apenas contra a
    UNIÃO de tokens conhecidos (:data:`deile.core.models.reasoning.KNOWN_EFFORTS`)
    — o suficiente para barrar typos sem conhecer o contexto no momento do load.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    stripped = value.strip().lower()
    if not stripped:
        return None
    # Lazy import — reasoning.py não importa nada de deile, mas mantemos o
    # padrão lazy de ``_to_optional_dispatcher`` por simetria/segurança.
    from deile.core.models.reasoning import is_valid_effort
    if not is_valid_effort(stripped):
        raise ValueError(
            f"invalid reasoning effort {stripped!r}; expected one of "
            "low/medium/high/xhigh/max/ultracode/auto (anthropic/claude) "
            "ou específico do provider (none/off/minimal/...)"
        )
    return stripped


# DNS-1123 label regex for validating deployment names in activity sources.
# Mirrors _POD_NAME_RE in infra/k8s/_panel_data.py — keep in sync.
# Allows single-character names (e.g. "x") and names up to 253 chars.
_ACTIVITY_DEPLOYMENT_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,251}[a-z0-9])?$")


def _to_activity_sources(value: Any) -> List[Dict[str, str]]:
    """Strict converter for ``panel.activity_sources`` entries (issue #447).

    Accepts a list of dicts, each with ``deployment`` (DNS-1123), ``role``
    (non-empty str) and ``color`` (non-empty str). Empty list → [] (sentinel
    for "use V1 default"). Duplicate ``deployment`` keys → rejected.
    ``role`` duplicates are allowed (cosmetic, not a security boundary).
    """
    if not isinstance(value, list):
        raise TypeError(f"expected list, got {type(value).__name__}")
    if not value:
        return []
    seen_deployments: set = set()
    result: List[Dict[str, str]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError(
                f"activity_sources[{idx}]: expected dict, got {type(item).__name__}"
            )
        deployment = item.get("deployment", "")
        if not isinstance(deployment, str) or not deployment:
            raise ValueError(
                f"activity_sources[{idx}].deployment: missing or empty"
            )
        if not _ACTIVITY_DEPLOYMENT_RE.match(deployment):
            raise ValueError(
                f"activity_sources[{idx}].deployment={deployment!r}: "
                "must match DNS-1123 (lowercase alphanumeric + hyphens, "
                "start/end with alphanumeric, max 253 chars)"
            )
        if deployment in seen_deployments:
            raise ValueError(
                f"activity_sources[{idx}].deployment={deployment!r}: duplicate"
            )
        seen_deployments.add(deployment)
        role = item.get("role", "")
        if not isinstance(role, str) or not role:
            raise ValueError(f"activity_sources[{idx}].role: missing or empty")
        color = item.get("color", "")
        if not isinstance(color, str) or not color:
            raise ValueError(f"activity_sources[{idx}].color: missing or empty")
        result.append({"deployment": deployment, "role": role, "color": color})
    return result


def _to_optional_positive_decimal(value: Any) -> Optional[Decimal]:
    """Strict converter for per-stage cost cap entries (issue #392).

    ``None`` and empty/whitespace string collapse to ``None`` (no cap).
    Non-strings, strings that don't parse as decimal, or non-positive values
    raise — ``apply_overrides`` catches and keeps the previous (default) value.
    Strict by design: a malformed cap would silently allow unlimited spend.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"cost cap must be finite, got {value!r}")
        if value <= 0:
            raise ValueError(f"cost cap must be positive, got {value}")
        return value
    if not isinstance(value, (str, int, float)):
        raise TypeError(f"expected str/numeric, got {type(value).__name__}")
    stripped = str(value).strip()
    if not stripped:
        return None
    try:
        d = Decimal(stripped)
    except InvalidOperation:
        raise ValueError(f"invalid decimal {stripped!r} for cost cap")
    if not d.is_finite():
        raise ValueError(f"cost cap must be finite, got {stripped!r}")
    if d <= 0:
        raise ValueError(f"cost cap must be positive, got {d}")
    return d
