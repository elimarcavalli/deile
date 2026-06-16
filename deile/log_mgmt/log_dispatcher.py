"""Dispatcher de workers investigativos para anomalias de log.

Quando o :class:`LogAnalyzer` detecta uma anomalia complexa e
``DEILE_LOG_AUTO_DISPATCH=true``, este módulo dispara um worker DEILE
(``persona: debugger``) para investigação profunda.

O worker lê contexto completo dos logs, cruza com código fonte (git
blame, commits recentes), diagnostica causa raiz, e abre issues ou
comenta em issues existentes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("deile.log_dispatcher")

# ── Config ──────────────────────────────────────────────────────────────────

_WORKER_ENDPOINT = os.environ.get("DEILE_WORKER_ENDPOINT", "http://deile-worker:8766")


def _get_worker_bearer() -> str:
    """Obtém o bearer token do worker via arquivo ou env var."""
    candidates = [
        "/run/secrets/worker/AUTH_TOKEN",
        os.environ.get("DEILE_WORKER_AUTH_TOKEN_FILE", ""),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                continue
    return os.environ.get("DEILE_WORKER_BEARER_TOKEN", "")


def _build_investigation_brief(anomalies: List[Dict[str, Any]]) -> str:
    """Constrói um brief de investigação a partir das anomalias detectadas.

    Args:
        anomalies: Lista de anomalias serializadas (to_dict).

    Returns:
        Brief em formato adequado para o worker DEILE investigar.
    """
    anomaly_summary = "\n".join(
        f"- {a.get('pattern_name', 'unknown')} "
        f"(severity={a.get('severity', 'unknown')}, "
        f"pod={a.get('pod_name', 'unknown')}, "
        f"count={a.get('count', '?')})"
        for a in anomalies
    )

    sample_text = ""
    for a in anomalies[:2]:
        if a.get("sample_lines"):
            sample_text += f"\nPod {a['pod_name']} samples:\n"
            for line in a["sample_lines"][:3]:
                sample_text += f"  {line}\n"

    return (
        f"# Investigação de Anomalias de Log\n\n"
        f"O LogAnalyzer detectou as seguintes anomalias:\n\n"
        f"{anomaly_summary}\n\n"
        f"## Amostras de Log\n\n"
        f"{sample_text}\n"
        f"## Tarefa\n\n"
        f"1. Analise os padrões de erro e identifique a causa raiz\n"
        f"2. Cruze com código fonte relevante (git blame, commits recentes)\n"
        f"3. Se for bug de código, corrija e abra PR\n"
        f"4. Se for problema de infra/config, documente e sugira ação\n"
        f"5. Reporte o diagnóstico completo\n\n"
        f"By [DEILE-One](mailto:deile@deile.info)\n"
    )


def dispatch_anomalies(
    anomalies: List[Any],
    *,
    worker_endpoint: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Dispara um worker DEILE para investigar as anomalias detectadas.

    Args:
        anomalies: Lista de :class:`Anomaly` ou dicts serializados.
        worker_endpoint: URL do worker (default: env var).
        bearer_token: Bearer token (default: via arquivo/env).

    Returns:
        Resposta do worker ou None se dispatch falhou.
    """
    if not anomalies:
        return None

    # Normaliza para dicts
    anomaly_dicts: List[Dict[str, Any]] = []
    for a in anomalies:
        if hasattr(a, "to_dict"):
            anomaly_dicts.append(a.to_dict())
        elif isinstance(a, dict):
            anomaly_dicts.append(a)
        else:
            anomaly_dicts.append({"pattern_name": str(a)})

    brief = _build_investigation_brief(anomaly_dicts)
    endpoint = worker_endpoint or _WORKER_ENDPOINT
    token = bearer_token or _get_worker_bearer()

    if not token:
        logger.warning(
            "log_dispatcher: no worker bearer token — cannot dispatch investigation"
        )
        return None

    # Tenta dispatch via HTTP (best-effort)
    try:
        import urllib.request

        payload = json.dumps(
            {
                "brief": brief,
                "channel_id": f"logger-auto-{os.environ.get('HOSTNAME', 'unknown')}",
                "persona": "debugger",
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{endpoint}/v1/dispatch",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            logger.info(
                "log_dispatcher: worker dispatched — task_id=%s", result.get("task_id")
            )
            return result

    except Exception as exc:
        logger.warning("log_dispatcher: dispatch failed: %s", exc)
        return None


def is_auto_dispatch_enabled() -> bool:
    """True se ``DEILE_LOG_AUTO_DISPATCH=true``."""
    return os.environ.get("DEILE_LOG_AUTO_DISPATCH", "false").lower() == "true"
