"""Pure Rich-rendering helpers for ``/logs`` subcommands.

Extracted from :class:`LogsCommand` to keep the command focused on
dispatch + side effects (audit_logger queries, export/clear mutations)
while the visual layer becomes independently testable. Same separation
already applied to ``/model`` (`_model_views.py`) and ``/cost``
(`_cost_views.py`).

Each helper takes plain data — no ``self``, no ``audit_logger``, no
registry — and returns a Rich renderable. The caller is responsible
for fetching that data from the right subsystem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Sequence

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...security.audit_logger import AuditEvent, AuditEventType, SeverityLevel
from ._shared import truncate

_APPROVAL_TYPES = (
    AuditEventType.APPROVAL_REQUIRED,
    AuditEventType.APPROVAL_GRANTED,
    AuditEventType.APPROVAL_DENIED,
)

_SEVERITY_LABEL = {
    SeverityLevel.DEBUG: "DEPURAÇÃO",
    SeverityLevel.INFO: "INFORMAÇÃO",
    SeverityLevel.WARNING: "AVISO",
    SeverityLevel.ERROR: "ERRO",
    SeverityLevel.CRITICAL: "CRÍTICO",
}

_SEVERITY_EMOJI = {
    SeverityLevel.DEBUG: "🔍",
    SeverityLevel.INFO: "ℹ️",
    SeverityLevel.WARNING: "⚠️",
    SeverityLevel.ERROR: "❌",
    SeverityLevel.CRITICAL: "🚨",
}

_TYPE_DESCRIPTIONS = {
    "permission_check": "Validação de controle de acesso",
    "permission_denied": "Eventos de acesso negado",
    "secret_detected": "Dados sensíveis encontrados",
    "secret_redacted": "Dados sanitizados",
    "tool_execution": "Eventos de execução de ferramentas",
    "plan_execution": "Eventos de fluxo de planos",
    "approval_required": "Aprovação manual necessária",
}


# --------------------------------------------------------------------- helpers


def format_time_ago(timestamp: datetime, *, now: datetime | None = None) -> str:
    """Compact "X ago" rendering for activity / security tables.

    Previously inlined in ``_show_logs_overview`` (seconds/minutes/HH:MM)
    and ``_show_security_logs`` (minutes "atrás"/HH:MM). Unified to a
    single helper — callers that need a more elaborate format can post-
    process the result.
    """
    base = now or datetime.now()
    diff = base - timestamp
    seconds = diff.total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return timestamp.strftime("%H:%M")


def format_event_type_label(
    event_type: AuditEventType, *, max_chars: int | None = None
) -> str:
    """``event_type.value`` → human-readable label.

    The pattern ``event_type.value.replace("_", " ").title()[:N]`` was
    repeated six times in ``logs_command.py``. Centralising prevents
    drift (e.g. one call site forgetting the ``.title()``).
    """
    label = event_type.value.replace("_", " ").title()
    if max_chars is not None:
        return label[:max_chars]
    return label


def _format_event_type_str(event_type_str: str, *, max_chars: int | None = None) -> str:
    """Same as :func:`format_event_type_label` but for raw string keys
    (used by ``build_types_table`` and ``build_summary_table`` which
    receive string-keyed dicts from ``audit_logger.get_security_summary``)."""
    label = event_type_str.replace("_", " ").title()
    if max_chars is not None:
        return label[:max_chars]
    return label


# ------------------------------------------------------------------- overview


def build_overview_table(
    *,
    total_events: int,
    session_id: str,
    permission_denials: int,
    secret_detections: int,
    recent_critical: int,
    log_file: str,
) -> Table:
    """Render the metric table for ``/logs`` (no args)."""
    table = Table(title="📊 Visão Geral dos Logs de Auditoria", show_header=False)
    table.add_column("Métrica", style="bold cyan")
    table.add_column("Valor", style="green")
    table.add_column("Detalhes", style="dim")

    table.add_row("Total de Eventos", str(total_events), "Na sessão atual")
    table.add_row("ID da Sessão", session_id, "Identificador da sessão atual")
    table.add_row(
        "Negativas de Permissão", str(permission_denials), "Eventos de acesso negado"
    )
    table.add_row(
        "Detecções de Segredos", str(secret_detections), "Dados sensíveis encontrados"
    )
    table.add_row("Eventos Críticos", str(recent_critical), "Erros e avisos")
    table.add_row("Arquivo de Log", str(log_file), "Local de armazenamento persistente")
    return table


def build_activity_table(recent_events: Sequence[AuditEvent]) -> Table | Panel:
    """Render the "Atividade Recente" section of ``/logs``.

    Returns a ``Panel`` when no events are available, otherwise a
    ``Table`` with the latest ten events.
    """
    if not recent_events:
        return Panel(
            Text("Nenhuma atividade recente para exibir.", style="dim"),
            title="⚡ Atividade Recente",
            border_style="dim",
        )

    table = Table(
        title="⚡ Atividade Recente (Últimos 10 eventos)",
        show_header=True,
        header_style="bold yellow",
    )
    table.add_column("Hora", style="cyan")
    table.add_column("Tipo", style="yellow")
    table.add_column("Ator", style="green")
    table.add_column("Ação", style="white")
    table.add_column("Recurso", style="blue")
    table.add_column("Resultado", style="red")

    now = datetime.now()
    for event in recent_events[:10]:
        time_str = format_time_ago(event.timestamp, now=now)
        actor = truncate(event.actor, 10)
        resource = truncate(event.resource, 18)
        if event.result in ("success", "allowed", "completed"):
            result_color = "green"
        elif event.result in ("denied", "failed", "error"):
            result_color = "red"
        else:
            result_color = "yellow"

        table.add_row(
            time_str,
            format_event_type_label(event.event_type, max_chars=12),
            actor,
            event.action.title(),
            resource,
            f"[{result_color}]{event.result}[/{result_color}]",
        )
    return table


def build_types_table(type_counts: Mapping[str, int]) -> Table | Panel:
    """Render the "Tipos de Evento" section of ``/logs``."""
    if not type_counts:
        return Panel(
            Text("Nenhum evento registrado ainda.", style="dim"),
            title="📋 Tipos de Evento",
            border_style="dim",
        )

    table = Table(
        title="📋 Tipos de Evento", show_header=True, header_style="bold blue"
    )
    table.add_column("Tipo de Evento", style="blue")
    table.add_column("Contagem", style="green", justify="center")
    table.add_column("Descrição", style="dim")

    for event_type, count in sorted(
        type_counts.items(), key=lambda x: x[1], reverse=True
    ):
        description = _TYPE_DESCRIPTIONS.get(event_type, "Evento personalizado")
        table.add_row(
            _format_event_type_str(event_type),
            str(count),
            description,
        )
    return table


def build_overview_help_panel() -> Panel:
    """Static usage panel shown at the bottom of ``/logs``."""
    return Panel(
        Text(
            "🚀 **Comandos Rápidos**\n\n"
            "/logs recent [N]        - Mostrar N eventos mais recentes\n"
            "/logs security          - Apenas eventos de segurança\n"
            "/logs permissions       - Verificações e negativas de permissão\n"
            "/logs secrets           - Eventos de detecção de segredos\n"
            "/logs tools             - Logs de execução de ferramentas\n"
            "/logs plans             - Logs de execução de planos\n"
            "/logs errors            - Apenas erros e avisos\n"
            "/logs summary           - Estatísticas detalhadas\n"
            "/logs export <arquivo>  - Exportar logs para arquivo\n\n"
            "📊 **Filtros Disponíveis**\n"
            "Tipo: permission, secret, tool, plan, approval\n"
            "Severidade: warning, error, critical\n"
            "Ator: nome_da_ferramenta, user, system",
            style="dim",
        ),
        title="📖 Guia de Uso",
        border_style="blue",
    )


def build_overview_group(
    *,
    summary: Mapping[str, Any],
    recent_events: Sequence[AuditEvent],
) -> Group:
    """Compose the full ``/logs`` overview view from a security summary
    + recent events. Keeps the command-side a single call."""
    overview = build_overview_table(
        total_events=summary.get("total_events", 0),
        session_id=summary.get("session_id", "—"),
        permission_denials=summary.get("permission_denials", 0),
        secret_detections=summary.get("secret_detections", 0),
        recent_critical=summary.get("recent_critical_events", 0),
        log_file=str(summary.get("log_file", "—")),
    )
    activity = build_activity_table(recent_events)
    types = build_types_table(summary.get("event_types", {}) or {})
    help_panel = build_overview_help_panel()
    return Group(overview, "", activity, "", types, "", help_panel)


# --------------------------------------------------------------------- recent


def build_recent_cap_warning(limit: int, safe_limit: int) -> Panel:
    """Yellow warning panel emitted when the user-requested limit was
    capped to ``MAX_SAFE_LIMIT``."""
    return Panel(
        Text(
            f"⚠️  Limite reduzido automaticamente de {limit} para {safe_limit} "
            "para proteger a memória.",
            style="yellow",
        ),
        title="Aviso de Limite",
        border_style="yellow",
    )


def build_recent_empty_panel() -> Panel:
    return Panel(
        Text("Nenhum evento de log encontrado.", style="yellow"),
        title="📄 Logs Recentes",
        border_style="yellow",
    )


def build_recent_logs_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(
        title=f"📄 Logs Recentes ({len(events)} eventos)",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Data/Hora", style="dim")
    table.add_column("Severidade", style="red")
    table.add_column("Tipo", style="yellow")
    table.add_column("Ator", style="green")
    table.add_column("Ação", style="white")
    table.add_column("Recurso", style="blue")
    table.add_column("Resultado", style="magenta")

    for event in events:
        emoji = _SEVERITY_EMOJI.get(event.severity, "📝")
        label = _SEVERITY_LABEL.get(event.severity, event.severity.value.upper())
        actor = truncate(event.actor, 10)
        resource = truncate(event.resource, 23)
        event_type = event.event_type.value.replace("_", " ")[:13]

        table.add_row(
            event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            f"{emoji} {label[:4]}",
            event_type,
            actor,
            event.action,
            resource,
            event.result,
        )
    return table


# ------------------------------------------------------------------- security


def build_security_empty_panel() -> Panel:
    return Panel(
        Text(
            "Nenhum evento de segurança encontrado. ✅ Sistema aparenta estar seguro.",
            style="green",
        ),
        title="🛡️ Logs de Segurança",
        border_style="green",
    )


def build_security_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(
        title=f"🛡️ Eventos de Segurança ({len(events)} total)",
        show_header=True,
        header_style="bold red",
    )
    table.add_column("Hora", style="cyan")
    table.add_column("Evento", style="red")
    table.add_column("Severidade", style="yellow")
    table.add_column("Detalhes", style="white")
    table.add_column("Ação Tomada", style="green")

    now = datetime.now()
    for event in events[:50]:
        diff_sec = (now - event.timestamp).total_seconds()
        # security tables use the explicit "Nm atrás" form when within
        # the hour; preserve the original wording (incl. "0m atrás" for
        # sub-minute deltas).
        if diff_sec < 3600:
            time_str = f"{int(diff_sec / 60)}m atrás"
        else:
            time_str = event.timestamp.strftime("%H:%M")

        if event.event_type == AuditEventType.PERMISSION_DENIED:
            details = f"{event.actor} → {event.resource} ({event.action})"
            action = "Acesso bloqueado"
        elif event.event_type in (
            AuditEventType.SECRET_DETECTED,
            AuditEventType.SECRET_REDACTED,
        ):
            secret_type = event.details.get("secret_type", "desconhecido")
            line = event.details.get("line_number", "?")
            details = f"{secret_type} em {event.resource}:{line}"
            action = (
                "Suprimido"
                if event.event_type == AuditEventType.SECRET_REDACTED
                else "Detectado"
            )
        else:
            details = f"{event.resource} por {event.actor}"
            action = event.result.title()

        table.add_row(
            time_str,
            format_event_type_label(event.event_type),
            event.severity.value.upper(),
            truncate(details, 38),
            action,
        )
    return table


# ----------------------------------------------------------------- permission


def build_permission_empty_panel() -> Panel:
    return Panel(
        Text("Nenhum evento de permissão encontrado.", style="blue"),
        title="🔐 Logs de Permissão",
        border_style="blue",
    )


def build_permission_stats_panel(*, total_checks: int, total_denials: int) -> Panel:
    denominator = total_checks + total_denials
    denial_rate = (total_denials / denominator * 100) if denominator > 0 else 0
    text = (
        f"**Estatísticas de Permissão**\n\n"
        f"Total de Verificações: {total_checks}\n"
        f"Negados: {total_denials}\n"
        f"Taxa de Negação: {denial_rate:.1f}%"
    )
    return Panel(Text(text, style="cyan"), title="📊 Estatísticas", border_style="cyan")


def build_permission_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(
        title="🔐 Eventos de Permissão (Últimos 30)",
        show_header=True,
        header_style="bold blue",
    )
    table.add_column("Hora", style="dim")
    table.add_column("Ferramenta", style="green")
    table.add_column("Recurso", style="blue")
    table.add_column("Ação", style="white")
    table.add_column("Resultado", style="red")
    table.add_column("Regra", style="yellow")

    for event in events[:30]:
        time_str = event.timestamp.strftime("%H:%M:%S")
        result_display = (
            "[green]✅ PERMITIDO[/green]"
            if event.result == "allowed"
            else "[red]❌ NEGADO[/red]"
        )
        rule_id = (event.details.get("rule_id") or "padrão")[:13]

        table.add_row(
            time_str,
            event.actor[:13],
            truncate(event.resource, 23),
            event.action,
            result_display,
            rule_id,
        )
    return table


def build_permission_group(
    *,
    permission_events: Sequence[AuditEvent],
    denied_events: Sequence[AuditEvent],
    combined_events: Sequence[AuditEvent],
) -> Group:
    stats = build_permission_stats_panel(
        total_checks=len(permission_events),
        total_denials=len(denied_events),
    )
    return Group(stats, "", build_permission_table(combined_events))


# --------------------------------------------------------------------- secrets


def build_secret_empty_panel() -> Panel:
    return Panel(
        Text(
            "Nenhum evento de detecção de segredos encontrado. "
            "✅ Nenhum dado sensível detectado.",
            style="green",
        ),
        title="🔐 Logs de Detecção de Segredos",
        border_style="green",
    )


def build_secret_logs_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(
        title=f"🔐 Eventos de Detecção de Segredos ({len(events)} total)",
        show_header=True,
        header_style="bold red",
    )
    table.add_column("Data/Hora", style="dim")
    table.add_column("Arquivo", style="blue")
    table.add_column("Tipo de Segredo", style="red")
    table.add_column("Linha", style="yellow", justify="center")
    table.add_column("Confiança", style="green", justify="center")
    table.add_column("Ação", style="magenta")

    for event in sorted(events, key=lambda e: e.timestamp, reverse=True):
        file_path = (
            event.resource.split("/")[-1] if "/" in event.resource else event.resource
        )
        secret_type = event.details.get("secret_type", "desconhecido")
        line_number = event.details.get("line_number", "?")
        confidence = event.details.get("confidence", 0.0)
        action = (
            "🔒 Suprimido"
            if event.event_type == AuditEventType.SECRET_REDACTED
            else "⚠️ Detectado"
        )

        table.add_row(
            event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            truncate(file_path, 23),
            secret_type.title(),
            str(line_number),
            f"{confidence:.2f}",
            action,
        )
    return table


# ----------------------------------------------------------------------- tools


def build_tool_empty_panel() -> Panel:
    return Panel(
        Text("Nenhum evento de execução de ferramenta encontrado.", style="blue"),
        title="🔧 Logs de Execução de Ferramentas",
        border_style="blue",
    )


def build_tool_logs_table(events: Sequence[AuditEvent]) -> Table:
    successful_runs = sum(1 for e in events if e.result == "success")
    success_rate = (successful_runs / len(events) * 100) if events else 0

    table = Table(
        title=(
            f"🔧 Logs de Execução de Ferramentas "
            f"({len(events)} execuções, {success_rate:.1f}% sucesso)"
        ),
        show_header=True,
        header_style="bold green",
    )
    table.add_column("Hora", style="dim")
    table.add_column("Ferramenta", style="green")
    table.add_column("Recurso", style="blue")
    table.add_column("Duração", style="yellow")
    table.add_column("Cód. Saída", style="cyan")
    table.add_column("Resultado", style="red")

    for event in sorted(events, key=lambda e: e.timestamp, reverse=True)[:30]:
        time_str = event.timestamp.strftime("%H:%M:%S")
        duration_ms = event.details.get("duration_ms")
        duration_str = f"{duration_ms}ms" if duration_ms else "N/D"
        exit_code = event.details.get("exit_code", "N/D")
        result_color = "green" if event.result == "success" else "red"
        result_display = f"[{result_color}]{event.result.upper()}[/{result_color}]"

        table.add_row(
            time_str,
            event.tool_name or event.actor,
            truncate(event.resource, 23),
            duration_str,
            str(exit_code),
            result_display,
        )
    return table


# ----------------------------------------------------------------------- plans


def build_plan_empty_panel() -> Panel:
    return Panel(
        Text("Nenhum evento de execução de plano encontrado.", style="blue"),
        title="📋 Logs de Execução de Planos",
        border_style="blue",
    )


def build_plan_logs_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(
        title=f"📋 Logs de Execução de Planos ({len(events)} eventos)",
        show_header=True,
        header_style="bold purple",
    )
    table.add_column("Hora", style="dim")
    table.add_column("ID do Plano", style="purple")
    table.add_column("Evento", style="yellow")
    table.add_column("Ação", style="white")
    table.add_column("Resultado", style="green")
    table.add_column("Detalhes", style="blue")

    for event in events[:30]:
        time_str = event.timestamp.strftime("%H:%M:%S")
        plan_id = truncate(event.plan_id or "N/D", 9)

        if event.event_type == AuditEventType.PLAN_EXECUTION:
            details = f"Etapas: {event.details.get('step_count', 'N/D')}"
        elif event.event_type in _APPROVAL_TYPES:
            step_id = event.details.get("step_id", "N/D")
            risk = event.details.get("risk_level", "N/D")
            details = f"Etapa: {step_id}, Risco: {risk}"
        else:
            details = "N/D"

        table.add_row(
            time_str,
            plan_id,
            format_event_type_label(event.event_type, max_chars=15),
            event.action.title(),
            event.result.title(),
            truncate(details, 23),
        )
    return table


# ---------------------------------------------------------------------- errors


def build_errors_empty_panel() -> Panel:
    return Panel(
        Text(
            "Nenhum erro ou aviso encontrado. ✅ Sistema funcionando normalmente.",
            style="green",
        ),
        title="❌ Logs de Erros",
        border_style="green",
    )


def build_errors_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(
        title=f"❌ Erros e Avisos ({len(events)} eventos)",
        show_header=True,
        header_style="bold red",
    )
    table.add_column("Data/Hora", style="dim")
    table.add_column("Severidade", style="red")
    table.add_column("Tipo", style="yellow")
    table.add_column("Ator", style="green")
    table.add_column("Detalhes do Erro", style="white")

    for event in events[:30]:
        emoji = _SEVERITY_EMOJI.get(event.severity, "❓")
        label = _SEVERITY_LABEL.get(event.severity, event.severity.value.upper())
        details = f"{event.resource} - {event.action} {event.result}"

        table.add_row(
            event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            f"{emoji} {label[:4]}",
            format_event_type_label(event.event_type, max_chars=15),
            event.actor[:12],
            truncate(details, 28),
        )
    return table


# --------------------------------------------------------------------- summary


def build_summary_table(events: Sequence[AuditEvent]) -> Table:
    table = Table(title="📊 Estatísticas Detalhadas de Auditoria", show_header=False)
    table.add_column("Métrica", style="bold cyan")
    table.add_column("Valor", style="green")
    table.add_column("Percentual", style="yellow")

    total_events = len(events)

    if not events:
        table.add_row("Total de Eventos", "0", "—")
        return table

    type_counts: Dict[str, int] = {}
    for event in events:
        key = event.event_type.value
        type_counts[key] = type_counts.get(key, 0) + 1

    for event_type, count in sorted(
        type_counts.items(), key=lambda x: x[1], reverse=True
    ):
        percentage = (count / total_events * 100) if total_events > 0 else 0
        table.add_row(
            _format_event_type_str(event_type),
            str(count),
            f"{percentage:.1f}%",
        )
    return table


# ----------------------------------------------------------------- export/clear


def build_export_success_panel(
    *, exported_file: str, format_type: str, event_count: int
) -> Panel:
    return Panel(
        Text(
            f"✅ **Logs Exportados com Sucesso**\n\n"
            f"**Arquivo**: {exported_file}\n"
            f"**Formato**: {format_type.upper()}\n"
            f"**Eventos**: {event_count}\n\n"
            f"O arquivo exportado contém todos os eventos de auditoria da sessão atual.",
            style="green",
        ),
        title="📤 Exportação Concluída",
        border_style="green",
    )


def build_clear_panel(*, count: int, log_file: str) -> Panel:
    return Panel(
        Text(
            f"✅ **Logs em Memória Limpos**\n\n"
            f"Removidos {count} eventos da memória.\n\n"
            f"Nota: Os logs persistentes em {log_file} são preservados.\n"
            f"Use ferramentas de sistema de arquivos para gerenciar o arquivo de log se necessário.",
            style="yellow",
        ),
        title="🗑️ Logs Limpos",
        border_style="yellow",
    )
