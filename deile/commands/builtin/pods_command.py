"""Pods Command — lista pods claude-worker do namespace ativo."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import wrap_command_errors

logger = logging.getLogger(__name__)

_KUBECTL_TIMEOUT = 5.0
_UTC = timezone.utc


def _format_age(age_s: float) -> str:
    """Format pod age in seconds to kubectl-like string (2d3h, 45m, 12s)."""
    secs = max(0, int(age_s))
    if secs < 60:
        return f"{secs}s"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 24:
        return f"{hours}h{rem_min}m" if rem_min else f"{hours}h"
    days = hours // 24
    rem_hr = hours % 24
    return f"{days}d{rem_hr}h" if rem_hr else f"{days}d"


def _parse_k8s_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse a Kubernetes RFC3339 timestamp to an aware UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _resolve_namespace() -> str:
    """Return the target namespace, reading from Settings singleton (pilar 03 §7)."""
    try:
        from ...config.settings import get_settings
        ns = get_settings().k8s_namespace
        return ns if ns else "deile"
    except Exception:
        return "deile"


async def _fetch_pods(kubectl: str, namespace: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run ``kubectl get pods`` and return (parsed_json, error_message)."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                kubectl,
                "-n", namespace,
                "get", "pods",
                "-l", "app=claude-worker",
                "-o", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=_KUBECTL_TIMEOUT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_KUBECTL_TIMEOUT)
    except asyncio.TimeoutError:
        return None, f"kubectl demorou mais de {_KUBECTL_TIMEOUT:.0f}s (timeout)"
    except OSError as exc:
        return None, f"Erro ao iniciar kubectl: {exc}"

    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace").strip()
        return None, f"kubectl retornou código {proc.returncode}: {err_text[:300]}"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"Resposta inválida do kubectl (JSON inválido): {exc}"

    return data, None


def _build_pods_table(items: List[Dict[str, Any]], namespace: str) -> Tuple[Table, int]:
    """Build the Rich table from raw pod ``items``, returning (table, ready_count)."""
    now = datetime.now(_UTC)
    table = Table(
        title=f"Pods claude-worker — namespace: {namespace}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Nome")
    table.add_column("Status", justify="center")
    table.add_column("Ready", justify="center")
    table.add_column("Restarts", justify="right")
    table.add_column("Idade", justify="right")

    ready_count = 0

    for item in items:
        meta = item.get("metadata", {})
        name = meta.get("name", "?")

        status = item.get("status", {})
        phase = status.get("phase", "Unknown")
        container_statuses = status.get("containerStatuses", []) or []
        is_ready = (
            all(cs.get("ready", False) for cs in container_statuses)
            if container_statuses
            else False
        )
        restarts = sum(cs.get("restartCount", 0) for cs in container_statuses)

        started_at = _parse_k8s_ts(status.get("startTime"))
        age_s = (now - started_at).total_seconds() if started_at else 0.0
        age_str = _format_age(age_s)

        is_healthy = phase == "Running" and is_ready
        if is_healthy:
            ready_count += 1

        ready_icon = "✓" if is_ready else "✗"
        row_style = "" if is_healthy else "yellow"

        table.add_row(
            Text(name, style=row_style),
            Text(phase, style=row_style),
            Text(ready_icon, style=row_style),
            Text(str(restarts), style=row_style),
            Text(age_str, style=row_style),
        )

    return table, ready_count


class PodsCommand(DirectCommand):
    """Lista pods claude-worker do namespace ativo com status, ready, restarts e idade."""

    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="pods",
            description="Lista pods claude-worker do namespace ativo (status, ready, restarts, idade).",
        )
        super().__init__(config)

    @wrap_command_errors("pods", message_template="Falha ao executar /{name}: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
        namespace = _resolve_namespace()

        kubectl = shutil.which("kubectl")
        if kubectl is None:
            return CommandResult.error_result(
                "kubectl não encontrado no PATH — instale kubectl para usar /pods."
            )

        data, err = await _fetch_pods(kubectl, namespace)
        if err is not None:
            return CommandResult.error_result(err)

        items = data.get("items", []) if data else []

        if not items:
            return CommandResult.success_result(
                f"Nenhum pod claude-worker encontrado no namespace {namespace}",
                "text",
            )

        table, ready_count = _build_pods_table(items, namespace)
        total = len(items)

        footer = Panel(
            Text(
                f"Namespace: {namespace}  |  {total} pods  |  {ready_count} Ready  |"
                f"  {total - ready_count} não-Ready",
                style="dim",
            ),
            border_style="dim",
        )

        return CommandResult.success_result(Group(table, footer), "rich")

    def get_help(self) -> str:
        return """Lista pods claude-worker do namespace ativo

Uso:
  /pods    Lista todos os pods com label app=claude-worker

Colunas:
  Nome       — nome completo do pod
  Status     — phase do pod (Running, Pending, Failed…)
  Ready      — ✓ se todos os containers estão ready, ✗ caso contrário
  Restarts   — total de restarts em todos os containers
  Idade      — tempo desde o startTime (ex: 2d3h, 45m, 12s)

Destaque:
  Linhas amarelas — pod não-Ready (phase ≠ Running ou container com ready=false)

Rodapé:
  Namespace | total de pods | ready/total

Namespace resolvido por DEILE_K8S_NAMESPACE (fallback: deile).

Comandos relacionados:
  /status    — visão geral do sistema
  /logs      — logs de auditoria"""
