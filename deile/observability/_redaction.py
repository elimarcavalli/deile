"""Tríade de redação de segredos — módulo-folha compartilhado.

Exporta ``_REDACT_RE``, ``_redact`` e ``_safe_attrs`` para uso em
``dispatch_export.py`` e ``dispatch_log_export.py``. Depende apenas de
stdlib (``re`` + ``typing``) — sem imports DEILE; ciclo impossível.
"""

from __future__ import annotations

import re
from typing import Any, Dict

__all__ = ["_REDACT_RE", "_redact", "_safe_attrs"]

_REDACT_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{36,}|glpat-[A-Za-z0-9_-]{20,}|gldt-[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|Bearer\s+\S{10,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[A-Z0-9]{16,}|[A-Za-z0-9+/]{40,}={0,2})",
    re.ASCII,
)


def _redact(value: str) -> str:
    """Substitui padrões de token/segredo por ``[REDACTED]``."""
    return _REDACT_RE.sub("[REDACTED]", value)


def _safe_attrs(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica redact em todos os valores string do dict."""
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        out[k] = _redact(str(v)) if isinstance(v, str) else v
    return out
