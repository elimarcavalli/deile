"""``/tasks`` command — visão consolidada das tarefas ativas no pipeline (issue #416).

Combina ``GET /v1/pipeline-status`` + ``GET /v1/pipeline-status/ledger`` via
``asyncio.gather`` e exibe um resumo legível em PT-BR das tarefas em andamento.

Uso:
    /tasks              visão padrão (status + ledger)
    /tasks --backlog    inclui também o backlog de itens pendentes
    /tasks --verbose    mostra task_id / session_id / attempt por item

Aliases: /tarefas

Implementação deliberadamente delgada — instancia
``ClusterObservabilityClient`` apontando para o pod ``deile-pipeline``
e exibe o que recebe. Nenhum dado é mutado (3 GETs, zero mutação).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict

from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import emit_audit_event, split_args, wrap_command_errors


def _fmt_age(ts_str: str | None) -> str:
    """Converte ISO-8601 / timestamp em '•Xmin atrás' ou '' se ausente."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            return "agora"
        if minutes == 1:
            return "há 1min"
        return f"há {minutes}min"
    except Exception:  # noqa: BLE001
        return ts_str[:16] if len(ts_str) >= 16 else ts_str


def _fmt_time(ts_str: str | None) -> str:
    """Extrai HH:MM:SS de um ISO-8601 ou retorna '—'."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return ts_str[:8] if len(ts_str) >= 8 else ts_str


def _build_output(
    status_data: Dict[str, Any],
    ledger_data: Dict[str, Any],
    *,
    backlog_data: Dict[str, Any] | None = None,
    verbose: bool = False,
) -> str:
    """Monta a saída de texto do /tasks a partir dos payloads do servidor."""
    lines: list[str] = []

    # --- Cabeçalho: status do pipeline ----------------------------------------
    ticks = status_data.get("ticks_total", 0)
    errors = status_data.get("errors_total", 0)
    last_tick = status_data.get("last_tick_at")
    next_tick = status_data.get("next_tick_at")
    pods = status_data.get("pods_seen") or []
    pods_str = ", ".join(str(p) for p in pods) if pods else "—"

    lines.append(
        f"🔄 Pipeline ativo — tick #{ticks}"
        f" (último: {_fmt_time(last_tick)}, próximo: ~{_fmt_time(next_tick)})"
    )
    lines.append(f"   Erros: {errors} | Pods: {pods_str}")
    lines.append("")

    # --- Ledger: tarefas em andamento -----------------------------------------
    raw_ledger = ledger_data.get("ledger") or {}
    if not isinstance(raw_ledger, dict):
        raw_ledger = {}

    active_items = list(raw_ledger.items())
    lines.append(f"📋 Em andamento ({len(active_items)}):")

    if not active_items:
        lines.append("   (nenhuma tarefa ativa)")
    else:
        for key, entry in active_items:
            if not isinstance(entry, dict):
                continue
            # key é "pr:N" ou "issue:N"
            kind, _, num = key.partition(":")
            kind = kind.strip()
            num = num.strip()
            label = f"{kind:<5} #{num}"

            stage = entry.get("stage") or entry.get("current_stage") or "—"
            started_at = entry.get("started_at") or entry.get("dispatched_at")
            age = _fmt_age(started_at)
            worker = entry.get("worker") or entry.get("dispatch_target") or "—"

            row = f"   {label}  {stage:<20}  {age:<10}  (worker: {worker})"

            if verbose:
                task_id = entry.get("task_id") or "—"
                session_id = entry.get("session_id") or "—"
                attempt = entry.get("attempt") or entry.get("reaper_attempt") or "—"
                row += f"\n         task_id={task_id}  session_id={session_id}  attempt={attempt}"

            lines.append(row)

    lines.append("")

    # --- Backlog (opcional) ----------------------------------------------------
    if backlog_data is not None:
        raw_backlog = backlog_data.get("backlog") or []
        lines.append(f"📥 Backlog ({len(raw_backlog)} itens):")
        if not raw_backlog:
            lines.append("   (backlog vazio)")
        else:
            for item in raw_backlog[:20]:  # teto de 20 para não inundar o terminal
                if isinstance(item, dict):
                    ref = item.get("ref") or item.get("number") or str(item)
                    kind = item.get("kind") or "item"
                    lines.append(f"   {kind} #{ref}")
                else:
                    lines.append(f"   {item}")
        lines.append("")

    # --- Rodapé ---------------------------------------------------------------
    if not verbose:
        lines.append("ℹ️  Use /tasks --verbose para task_id/session_id/attempt")

    return "\n".join(lines)


class TasksCommand(DirectCommand):
    """``/tasks`` — mostra as tarefas ativas no pipeline (issue #416)."""

    cli_flag = "--tasks"
    cli_help = "Exibe tarefas ativas no pipeline autônomo e encerra."
    cli_requires_provider = False

    def __init__(self) -> None:
        from ...config.manager import CommandConfig

        config = CommandConfig(
            name="tasks",
            description=(
                "Visão consolidada das tarefas ativas no pipeline "
                "(combina /v1/pipeline-status + /v1/pipeline-status/ledger)."
            ),
            action="tasks",
            aliases=["tarefas"],
        )
        super().__init__(config)
        self.category = "orchestration"

    @wrap_command_errors("tasks", message_template="Falha ao executar /tasks: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
        from ...security.audit_logger import AuditEventType, SeverityLevel

        emit_audit_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=SeverityLevel.INFO,
            resource="/tasks",
            action="execute",
            details={"args": context.args},
        )

        try:
            from ...ui.panel.observability import client as obs_client
        except ImportError as exc:
            return CommandResult.error_result(
                f"módulo de observabilidade não disponível: {exc}",
            )

        # --- Flags -----------------------------------------------------------
        parts = split_args(context)
        want_backlog = "--backlog" in parts
        verbose = "--verbose" in parts

        # --- Credenciais e endpoint ------------------------------------------
        pipeline_endpoint = os.environ.get(
            "DEILE_PIPELINE_STATUS_ENDPOINT",
            "http://deile-pipeline-status:8768",
        )
        claude_worker_endpoint = os.environ.get(
            "DEILE_CLAUDE_WORKER_ENDPOINT",
            "http://claude-worker:8767",
        )
        pipeline_token = (
            os.environ.get("DEILE_PIPELINE_STATUS_AUTH_TOKEN", "").strip() or None
        )
        # Fallback para secret montado em /run/secrets/
        if pipeline_token is None:
            secret_path = "/run/secrets/pipeline-status/AUTH_TOKEN"
            try:
                pipeline_token = (
                    open(secret_path).read().strip() or None
                )  # noqa: WPS515
            except OSError:
                pass

        claude_worker_token = (
            os.environ.get("DEILE_CLAUDE_WORKER_AUTH_TOKEN", "").strip() or None
        )

        # --- Construção do cliente -------------------------------------------
        try:
            cli = obs_client.ClusterObservabilityClient.from_endpoints(
                pipeline_url=pipeline_endpoint,
                pipeline_token=pipeline_token,
                claude_worker_url=claude_worker_endpoint,
                claude_worker_token=claude_worker_token,
            )
        except Exception as exc:  # noqa: BLE001
            return CommandResult.error_result(
                f"falha ao criar ClusterObservabilityClient: {exc}",
            )

        # --- Chamadas concorrentes ao servidor -------------------------------
        if want_backlog:
            status_reply, ledger_reply, backlog_reply = await asyncio.gather(
                cli.pipeline.get_status(),
                cli.pipeline.get_ledger(),
                cli.pipeline.get_backlog(),
            )
        else:
            status_reply, ledger_reply = await asyncio.gather(
                cli.pipeline.get_status(),
                cli.pipeline.get_ledger(),
            )
            backlog_reply = None

        # --- Tratamento de ApiError ------------------------------------------
        if isinstance(status_reply, obs_client.ApiError):
            code = status_reply.status
            if code == 0:
                return CommandResult.error_result(
                    "pipeline-status inacessível (sem conexão ou timeout). "
                    "Verifique se o pod deile-pipeline está em execução.",
                )
            if code == 401:
                return CommandResult.error_result(
                    "pipeline-status: autenticação falhou (HTTP 401). "
                    "Verifique DEILE_PIPELINE_STATUS_AUTH_TOKEN.",
                )
            return CommandResult.error_result(
                f"pipeline-status: erro HTTP {code} — {status_reply.message}",
            )

        if isinstance(ledger_reply, obs_client.ApiError):
            code = ledger_reply.status
            if code == 0:
                return CommandResult.error_result(
                    "pipeline-status/ledger inacessível (sem conexão ou timeout).",
                )
            if code == 401:
                return CommandResult.error_result(
                    "pipeline-status/ledger: autenticação falhou (HTTP 401). "
                    "Verifique DEILE_PIPELINE_STATUS_AUTH_TOKEN.",
                )
            return CommandResult.error_result(
                f"pipeline-status/ledger: erro HTTP {code} — {ledger_reply.message}",
            )

        if backlog_reply is not None and isinstance(backlog_reply, obs_client.ApiError):
            # Backlog é opcional — degradamos graciosamente em vez de abortar
            backlog_data = None
        else:
            backlog_data = backlog_reply if want_backlog else None

        # --- Montagem da saída -----------------------------------------------
        status_data = status_reply if isinstance(status_reply, dict) else {}
        ledger_data = ledger_reply if isinstance(ledger_reply, dict) else {}
        backlog_dict = backlog_data if isinstance(backlog_data, dict) else None

        output = _build_output(
            status_data,
            ledger_data,
            backlog_data=backlog_dict,
            verbose=verbose,
        )

        return CommandResult.success_result(content=output, content_type="text")
