"""Status data collectors — pure observation, no Rich/UI concerns.

Extracted from ``status_command.py`` so the command file owns dispatch
and presentation while these helpers own subsystem polling. Each
collector returns a plain ``dict`` (with an ``error`` key on failure)
that the panel builders project into Rich panels.

Pillar 03 §1: I/O is fenced behind explicit collectors so async/sync
boundaries stay obvious. Pillar 03 §6: every collector returns instead
of raising — failures are surfaced as ``{"error": ...}`` and never abort
the status overview.
"""

from __future__ import annotations

import platform
import sys
from datetime import datetime
from typing import Any, Dict, List

import psutil

from deile.__version__ import __version__


def _indisponivel(reason: str) -> str:
    return f"[INDISPONÍVEL: {reason}]"


def get_system_uptime() -> str:
    """System uptime in ``Nd Hh Mm`` format; ``"desconhecido"`` on error."""
    try:
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        delta = datetime.now() - boot_time
        h, rem = divmod(delta.seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{delta.days}d {h}h {m}m"
    except Exception:
        return "desconhecido"


def collect_system_info() -> Dict[str, Any]:
    """Host + DEILE-version + memory snapshot for the System panel."""
    try:
        return {
            "deile_version": __version__,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
            "uptime": get_system_uptime(),
            "cpu_count": psutil.cpu_count(),
            "memory_total": psutil.virtual_memory().total,
            "memory_used": psutil.virtual_memory().used,
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage(".").percent,
        }
    except Exception as exc:
        return {"error": str(exc)}


def collect_models_info() -> Dict[str, Any]:
    """Active model + provider snapshot from the legacy ModelRouter."""
    try:
        from deile.core.models.router import get_model_router
        router = get_model_router()
        providers = list(router.providers.keys())
        active_key = providers[0] if providers else None
        active_provider = (
            active_key.split(":", 1)[0] if active_key
            else _indisponivel("nenhum provedor")
        )
        active_model = (
            active_key.split(":", 1)[1] if active_key and ":" in active_key
            else (active_key or _indisponivel("nenhum modelo"))
        )
        return {
            "active_model": active_model,
            "active_provider": active_provider,
            "total_providers": len(providers),
            "providers": providers,
        }
    except Exception as exc:
        return {"error": str(exc)}


def collect_tools_info() -> Dict[str, Any]:
    """Tool registry stats — counts by category and a flat name list."""
    try:
        from deile.tools.registry import get_tool_registry
        registry = get_tool_registry()
        stats = registry.get_stats()
        return {
            "total_tools": stats["total_tools"],
            "enabled_tools": stats["enabled_tools"],
            "disabled_tools": stats["disabled_tools"],
            "categories": stats["categories"],
            "function_definitions": stats["available_functions"],
            "tools_with_schemas": stats["tools_with_schemas"],
            "auto_discovery": stats["auto_discovery_enabled"],
            "tool_names": [t.name for t in registry.list_all()],
        }
    except Exception as exc:
        return {"error": str(exc)}


def collect_health_info() -> Dict[str, Any]:
    """CPU/memory health score — same heuristic as before refactor."""
    try:
        cpu_percent = psutil.cpu_percent(interval=0)
        memory = psutil.virtual_memory()
        health_score = 100
        warnings: List[str] = []
        if cpu_percent > 80:
            health_score -= 20
            warnings.append("CPU alto")
        if memory.percent > 85:
            health_score -= 15
            warnings.append("Memória alta")
        status = (
            "saudável" if health_score >= 80
            else "atenção" if health_score >= 60
            else "crítico"
        )
        return {
            "overall_status": status,
            "health_score": health_score,
            "cpu_usage": cpu_percent,
            "memory_usage": memory.percent,
            "warnings": warnings,
            "uptime": get_system_uptime(),
            "last_check": datetime.now().isoformat(),
        }
    except Exception as exc:
        return {"error": str(exc), "overall_status": "desconhecido"}
