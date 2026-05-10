"""Logs Command — Visualização de logs de auditoria de segurança e eventos do sistema."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ...security.audit_logger import (AuditEvent, AuditEventType,
                                      SeverityLevel, get_audit_logger)
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import split_args, truncate

logger = logging.getLogger(__name__)

MAX_SAFE_LIMIT = 500

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

_SEVERITY_FILTER_MAP: Dict[str, List[SeverityLevel]] = {
    "warning": [SeverityLevel.WARNING],
    "error": [SeverityLevel.ERROR],
    "critical": [SeverityLevel.CRITICAL],
}

_VALID_EXPORT_FORMATS = {"json", "csv"}

_TYPE_DESCRIPTIONS = {
    "permission_check": "Validação de controle de acesso",
    "permission_denied": "Eventos de acesso negado",
    "secret_detected": "Dados sensíveis encontrados",
    "secret_redacted": "Dados sanitizados",
    "tool_execution": "Eventos de execução de ferramentas",
    "plan_execution": "Eventos de fluxo de planos",
    "approval_required": "Aprovação manual necessária",
}


class LogsCommand(DirectCommand):
    """Visualizar logs de auditoria de segurança e eventos do sistema."""

    cli_flag = "--logs"
    cli_help = "Visualizar logs de auditoria e eventos do sistema."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="logs",
            description="Visualizar logs de auditoria de segurança e eventos do sistema.",
        )
        super().__init__(config)
        self.audit_logger = get_audit_logger()

    async def execute(self, context: CommandContext) -> CommandResult:
        try:
            parts = split_args(context)

            if not parts:
                return await self._show_logs_overview()

            action = parts[0].lower()

            if action == "recent":
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
                return await self._show_recent_logs(limit)
            elif action == "security":
                return await self._show_security_logs(parts[1:])
            elif action == "permissions":
                return await self._show_permission_logs(parts[1:])
            elif action == "secrets":
                return await self._show_secret_logs(parts[1:])
            elif action == "tools":
                return await self._show_tool_logs(parts[1:])
            elif action == "plans":
                return await self._show_plan_logs(parts[1:])
            elif action == "errors":
                return await self._show_error_logs(parts[1:])
            elif action == "summary":
                return await self._show_summary()
            elif action == "export":
                if len(parts) < 2:
                    raise CommandError("logs export requer nome de arquivo: /logs export <arquivo> [formato]")
                safe_name = Path(parts[1]).name
                if not safe_name:
                    raise CommandError("Nome de arquivo inválido")
                format_type = parts[2] if len(parts) > 2 else "json"
                if format_type not in _VALID_EXPORT_FORMATS:
                    raise CommandError(f"Formato inválido. Use: {', '.join(sorted(_VALID_EXPORT_FORMATS))}")
                return await self._export_logs(safe_name, format_type)
            elif action == "clear":
                return await self._clear_logs()
            else:
                raise CommandError(f"Ação desconhecida: {action}")

        except CommandError:
            raise
        except Exception as e:
            logger.error("Falha inesperada no comando logs: %s", e, exc_info=True)
            raise CommandError(f"Falha ao executar comando logs: {str(e)}")

    async def _show_logs_overview(self) -> CommandResult:
        summary = self.audit_logger.get_security_summary()
        if not isinstance(summary, dict):
            summary = {}

        total_events = summary.get("total_events", 0)
        session_id = summary.get("session_id", "—")
        permission_denials = summary.get("permission_denials", 0)
        secret_detections = summary.get("secret_detections", 0)
        recent_critical = summary.get("recent_critical_events", 0)
        log_file = summary.get("log_file", "—")

        recent_events = self.audit_logger.get_recent_events(20)

        overview_table = Table(title="📊 Visão Geral dos Logs de Auditoria", show_header=False)
        overview_table.add_column("Métrica", style="bold cyan", width=22)
        overview_table.add_column("Valor", style="green", width=15)
        overview_table.add_column("Detalhes", style="dim", width=30)

        overview_table.add_row("Total de Eventos", str(total_events), "Na sessão atual")
        overview_table.add_row("ID da Sessão", session_id, "Identificador da sessão atual")
        overview_table.add_row("Negativas de Permissão", str(permission_denials), "Eventos de acesso negado")
        overview_table.add_row("Detecções de Segredos", str(secret_detections), "Dados sensíveis encontrados")
        overview_table.add_row("Eventos Críticos", str(recent_critical), "Erros e avisos")
        overview_table.add_row("Arquivo de Log", str(log_file), "Local de armazenamento persistente")

        if recent_events:
            activity_table = Table(
                title="⚡ Atividade Recente (Últimos 10 eventos)",
                show_header=True,
                header_style="bold yellow",
            )
            activity_table.add_column("Hora", style="cyan", width=8)
            activity_table.add_column("Tipo", style="yellow", width=12)
            activity_table.add_column("Ator", style="green", width=12)
            activity_table.add_column("Ação", style="white", width=10)
            activity_table.add_column("Recurso", style="blue", width=20)
            activity_table.add_column("Resultado", style="red", width=10)

            for event in recent_events[:10]:
                time_diff = datetime.now() - event.timestamp
                if time_diff.total_seconds() < 60:
                    time_str = f"{int(time_diff.total_seconds())}s"
                elif time_diff.total_seconds() < 3600:
                    time_str = f"{int(time_diff.total_seconds() / 60)}m"
                else:
                    time_str = event.timestamp.strftime("%H:%M")

                actor = truncate(event.actor, 10)
                resource = truncate(event.resource, 18)
                result_color = (
                    "green" if event.result in ("success", "allowed", "completed")
                    else "red" if event.result in ("denied", "failed", "error")
                    else "yellow"
                )

                activity_table.add_row(
                    time_str,
                    event.event_type.value.replace("_", " ").title()[:12],
                    actor,
                    event.action.title(),
                    resource,
                    f"[{result_color}]{event.result}[/{result_color}]",
                )
        else:
            activity_table = Panel(
                Text("Nenhuma atividade recente para exibir.", style="dim"),
                title="⚡ Atividade Recente",
                border_style="dim",
            )

        type_counts = summary.get("event_types", {})
        if type_counts:
            types_table = Table(title="📋 Tipos de Evento", show_header=True, header_style="bold blue")
            types_table.add_column("Tipo de Evento", style="blue")
            types_table.add_column("Contagem", style="green", justify="center")
            types_table.add_column("Descrição", style="dim")

            for event_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
                description = _TYPE_DESCRIPTIONS.get(event_type, "Evento personalizado")
                types_table.add_row(
                    event_type.replace("_", " ").title(),
                    str(count),
                    description,
                )
        else:
            types_table = Panel(
                Text("Nenhum evento registrado ainda.", style="dim"),
                title="📋 Tipos de Evento",
                border_style="dim",
            )

        commands_panel = Panel(
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

        content = Group(overview_table, "", activity_table, "", types_table, "", commands_panel)
        return CommandResult.success_result(content, "rich")

    async def _show_recent_logs(self, limit: int) -> CommandResult:
        effective_limit = min(limit, MAX_SAFE_LIMIT)
        capped = limit > MAX_SAFE_LIMIT

        events = self.audit_logger.get_recent_events(effective_limit)

        renderables: List = []

        if capped:
            renderables.append(Panel(
                Text(
                    f"⚠️  Limite reduzido automaticamente de {limit} para {MAX_SAFE_LIMIT} "
                    "para proteger a memória.",
                    style="yellow",
                ),
                title="Aviso de Limite",
                border_style="yellow",
            ))

        if not events:
            renderables.append(Panel(
                Text("Nenhum evento de log encontrado.", style="yellow"),
                title="📄 Logs Recentes",
                border_style="yellow",
            ))
            return CommandResult.success_result(Group(*renderables), "rich")

        log_table = Table(
            title=f"📄 Logs Recentes ({len(events)} eventos)",
            show_header=True,
            header_style="bold cyan",
        )
        log_table.add_column("Data/Hora", style="dim", width=19)
        log_table.add_column("Severidade", style="red", width=12)
        log_table.add_column("Tipo", style="yellow", width=15)
        log_table.add_column("Ator", style="green", width=12)
        log_table.add_column("Ação", style="white", width=10)
        log_table.add_column("Recurso", style="blue", width=25)
        log_table.add_column("Resultado", style="magenta", width=10)

        for event in events:
            emoji = _SEVERITY_EMOJI.get(event.severity, "📝")
            label = _SEVERITY_LABEL.get(event.severity, event.severity.value.upper())
            actor = truncate(event.actor, 10)
            resource = truncate(event.resource, 23)
            event_type = event.event_type.value.replace("_", " ")[:13]

            log_table.add_row(
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                f"{emoji} {label[:4]}",
                event_type,
                actor,
                event.action,
                resource,
                event.result,
            )

        renderables.append(log_table)
        return CommandResult.success_result(Group(*renderables), "rich")

    async def _show_security_logs(self, _filters: List[str]) -> CommandResult:
        security_event_types = [
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SECRET_DETECTED,
            AuditEventType.SECRET_REDACTED,
            AuditEventType.SUSPICIOUS_ACTIVITY,
        ]

        all_events = []
        for event_type in security_event_types:
            all_events.extend(self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=event_type))
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not all_events:
            return CommandResult.success_result(
                Panel(
                    Text("Nenhum evento de segurança encontrado. ✅ Sistema aparenta estar seguro.", style="green"),
                    title="🛡️ Logs de Segurança",
                    border_style="green",
                ),
                "rich",
            )

        security_table = Table(
            title=f"🛡️ Eventos de Segurança ({len(all_events)} total)",
            show_header=True,
            header_style="bold red",
        )
        security_table.add_column("Hora", style="cyan", width=8)
        security_table.add_column("Evento", style="red", width=15)
        security_table.add_column("Severidade", style="yellow", width=10)
        security_table.add_column("Detalhes", style="white", width=40)
        security_table.add_column("Ação Tomada", style="green", width=15)

        for event in all_events[:50]:
            time_diff = datetime.now() - event.timestamp
            time_str = (
                f"{int(time_diff.total_seconds() / 60)}m atrás"
                if time_diff.total_seconds() < 3600
                else event.timestamp.strftime("%H:%M")
            )

            if event.event_type == AuditEventType.PERMISSION_DENIED:
                details = f"{event.actor} → {event.resource} ({event.action})"
                action = "Acesso bloqueado"
            elif event.event_type in (AuditEventType.SECRET_DETECTED, AuditEventType.SECRET_REDACTED):
                secret_type = event.details.get("secret_type", "desconhecido")
                line = event.details.get("line_number", "?")
                details = f"{secret_type} em {event.resource}:{line}"
                action = "Suprimido" if event.event_type == AuditEventType.SECRET_REDACTED else "Detectado"
            else:
                details = f"{event.resource} por {event.actor}"
                action = event.result.title()

            security_table.add_row(
                time_str,
                event.event_type.value.replace("_", " ").title(),
                event.severity.value.upper(),
                truncate(details, 38),
                action,
            )

        return CommandResult.success_result(security_table, "rich")

    async def _show_permission_logs(self, _filters: List[str]) -> CommandResult:
        permission_events = self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=AuditEventType.PERMISSION_CHECK)
        denied_events = self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=AuditEventType.PERMISSION_DENIED)

        all_events = permission_events + denied_events
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not all_events:
            return CommandResult.success_result(
                Panel(
                    Text("Nenhum evento de permissão encontrado.", style="blue"),
                    title="🔐 Logs de Permissão",
                    border_style="blue",
                ),
                "rich",
            )

        total_checks = len(permission_events)
        total_denials = len(denied_events)
        denial_rate = (total_denials / (total_checks + total_denials) * 100) if (total_checks + total_denials) > 0 else 0

        stats_text = (
            f"**Estatísticas de Permissão**\n\n"
            f"Total de Verificações: {total_checks}\n"
            f"Negados: {total_denials}\n"
            f"Taxa de Negação: {denial_rate:.1f}%"
        )

        stats_panel = Panel(Text(stats_text, style="cyan"), title="📊 Estatísticas", border_style="cyan")

        perm_table = Table(
            title="🔐 Eventos de Permissão (Últimos 30)",
            show_header=True,
            header_style="bold blue",
        )
        perm_table.add_column("Hora", style="dim", width=8)
        perm_table.add_column("Ferramenta", style="green", width=15)
        perm_table.add_column("Recurso", style="blue", width=25)
        perm_table.add_column("Ação", style="white", width=10)
        perm_table.add_column("Resultado", style="red", width=10)
        perm_table.add_column("Regra", style="yellow", width=15)

        for event in all_events[:30]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            result_display = (
                "[green]✅ PERMITIDO[/green]" if event.result == "allowed"
                else "[red]❌ NEGADO[/red]"
            )
            rule_id = (event.details.get("rule_id") or "padrão")[:13]

            perm_table.add_row(
                time_str,
                event.actor[:13],
                truncate(event.resource, 23),
                event.action,
                result_display,
                rule_id,
            )

        return CommandResult.success_result(Group(stats_panel, "", perm_table), "rich")

    async def _show_secret_logs(self, _filters: List[str]) -> CommandResult:
        secret_events: List[AuditEvent] = []
        for event_type in (AuditEventType.SECRET_DETECTED, AuditEventType.SECRET_REDACTED):
            secret_events.extend(self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=event_type))

        if not secret_events:
            return CommandResult.success_result(
                Panel(
                    Text(
                        "Nenhum evento de detecção de segredos encontrado. ✅ Nenhum dado sensível detectado.",
                        style="green",
                    ),
                    title="🔐 Logs de Detecção de Segredos",
                    border_style="green",
                ),
                "rich",
            )

        secrets_table = Table(
            title=f"🔐 Eventos de Detecção de Segredos ({len(secret_events)} total)",
            show_header=True,
            header_style="bold red",
        )
        secrets_table.add_column("Data/Hora", style="dim", width=19)
        secrets_table.add_column("Arquivo", style="blue", width=25)
        secrets_table.add_column("Tipo de Segredo", style="red", width=15)
        secrets_table.add_column("Linha", style="yellow", width=6, justify="center")
        secrets_table.add_column("Confiança", style="green", width=10, justify="center")
        secrets_table.add_column("Ação", style="magenta", width=12)

        for event in sorted(secret_events, key=lambda e: e.timestamp, reverse=True):
            file_path = event.resource.split("/")[-1] if "/" in event.resource else event.resource
            secret_type = event.details.get("secret_type", "desconhecido")
            line_number = event.details.get("line_number", "?")
            confidence = event.details.get("confidence", 0.0)
            action = "🔒 Suprimido" if event.event_type == AuditEventType.SECRET_REDACTED else "⚠️ Detectado"

            secrets_table.add_row(
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                truncate(file_path, 23),
                secret_type.title(),
                str(line_number),
                f"{confidence:.2f}",
                action,
            )

        return CommandResult.success_result(secrets_table, "rich")

    async def _show_tool_logs(self, _filters: List[str]) -> CommandResult:
        tool_events = self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=AuditEventType.TOOL_EXECUTION)

        if not tool_events:
            return CommandResult.success_result(
                Panel(
                    Text("Nenhum evento de execução de ferramenta encontrado.", style="blue"),
                    title="🔧 Logs de Execução de Ferramentas",
                    border_style="blue",
                ),
                "rich",
            )

        successful_runs = sum(1 for e in tool_events if e.result == "success")
        success_rate = (successful_runs / len(tool_events) * 100) if tool_events else 0

        tools_table = Table(
            title=f"🔧 Logs de Execução de Ferramentas ({len(tool_events)} execuções, {success_rate:.1f}% sucesso)",
            show_header=True,
            header_style="bold green",
        )
        tools_table.add_column("Hora", style="dim", width=8)
        tools_table.add_column("Ferramenta", style="green", width=15)
        tools_table.add_column("Recurso", style="blue", width=25)
        tools_table.add_column("Duração", style="yellow", width=10)
        tools_table.add_column("Cód. Saída", style="cyan", width=10)
        tools_table.add_column("Resultado", style="red", width=10)

        for event in sorted(tool_events, key=lambda e: e.timestamp, reverse=True)[:30]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            duration_ms = event.details.get("duration_ms")
            duration_str = f"{duration_ms}ms" if duration_ms else "N/D"
            exit_code = event.details.get("exit_code", "N/D")
            result_color = "green" if event.result == "success" else "red"
            result_display = f"[{result_color}]{event.result.upper()}[/{result_color}]"

            tools_table.add_row(
                time_str,
                event.tool_name or event.actor,
                truncate(event.resource, 23),
                duration_str,
                str(exit_code),
                result_display,
            )

        return CommandResult.success_result(tools_table, "rich")

    async def _show_plan_logs(self, _filters: List[str]) -> CommandResult:
        plan_events = self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=AuditEventType.PLAN_EXECUTION)
        approval_events: List[AuditEvent] = [
            e
            for et in _APPROVAL_TYPES
            for e in self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=et)
        ]

        all_events = plan_events + approval_events
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not all_events:
            return CommandResult.success_result(
                Panel(
                    Text("Nenhum evento de execução de plano encontrado.", style="blue"),
                    title="📋 Logs de Execução de Planos",
                    border_style="blue",
                ),
                "rich",
            )

        plans_table = Table(
            title=f"📋 Logs de Execução de Planos ({len(all_events)} eventos)",
            show_header=True,
            header_style="bold purple",
        )
        plans_table.add_column("Hora", style="dim", width=8)
        plans_table.add_column("ID do Plano", style="purple", width=12)
        plans_table.add_column("Evento", style="yellow", width=15)
        plans_table.add_column("Ação", style="white", width=12)
        plans_table.add_column("Resultado", style="green", width=12)
        plans_table.add_column("Detalhes", style="blue", width=25)

        for event in all_events[:30]:
            time_str = event.timestamp.strftime("%H:%M:%S")
            plan_id = event.plan_id or "N/D"
            if len(plan_id) > 12:
                plan_id = plan_id[:9] + "..."

            event_type_label = event.event_type.value.replace("_", " ").title()

            if event.event_type == AuditEventType.PLAN_EXECUTION:
                details = f"Etapas: {event.details.get('step_count', 'N/D')}"
            elif event.event_type in _APPROVAL_TYPES:
                step_id = event.details.get("step_id", "N/D")
                risk = event.details.get("risk_level", "N/D")
                details = f"Etapa: {step_id}, Risco: {risk}"
            else:
                details = "N/D"

            plans_table.add_row(
                time_str,
                plan_id,
                event_type_label[:15],
                event.action.title(),
                event.result.title(),
                truncate(details, 23),
            )

        return CommandResult.success_result(plans_table, "rich")

    async def _show_error_logs(self, filters: List[str]) -> CommandResult:
        all_severities = [SeverityLevel.WARNING, SeverityLevel.ERROR, SeverityLevel.CRITICAL]

        target_severities = all_severities
        if "--severity" in filters:
            idx = filters.index("--severity")
            if idx + 1 < len(filters):
                filter_val = filters[idx + 1].lower()
                mapped = _SEVERITY_FILTER_MAP.get(filter_val)
                if mapped is None:
                    valid = ", ".join(sorted(_SEVERITY_FILTER_MAP))
                    raise CommandError(f"Severidade inválida '{filter_val}'. Use: {valid}")
                target_severities = mapped

        error_events: List[AuditEvent] = []
        for severity in target_severities:
            error_events.extend(self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, severity=severity))
        error_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not error_events:
            return CommandResult.success_result(
                Panel(
                    Text("Nenhum erro ou aviso encontrado. ✅ Sistema funcionando normalmente.", style="green"),
                    title="❌ Logs de Erros",
                    border_style="green",
                ),
                "rich",
            )

        errors_table = Table(
            title=f"❌ Erros e Avisos ({len(error_events)} eventos)",
            show_header=True,
            header_style="bold red",
        )
        errors_table.add_column("Data/Hora", style="dim", width=19)
        errors_table.add_column("Severidade", style="red", width=12)
        errors_table.add_column("Tipo", style="yellow", width=15)
        errors_table.add_column("Ator", style="green", width=12)
        errors_table.add_column("Detalhes do Erro", style="white", width=30)

        for event in error_events[:30]:
            emoji = _SEVERITY_EMOJI.get(event.severity, "❓")
            label = _SEVERITY_LABEL.get(event.severity, event.severity.value.upper())
            details = f"{event.resource} - {event.action} {event.result}"

            errors_table.add_row(
                event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                f"{emoji} {label[:4]}",
                event.event_type.value.replace("_", " ").title()[:15],
                event.actor[:12],
                truncate(details, 28),
            )

        return CommandResult.success_result(errors_table, "rich")

    async def _show_summary(self) -> CommandResult:
        recent_events = self.audit_logger.get_recent_events(MAX_SAFE_LIMIT)

        stats_table = Table(title="📊 Estatísticas Detalhadas de Auditoria", show_header=False)
        stats_table.add_column("Métrica", style="bold cyan", width=25)
        stats_table.add_column("Valor", style="green", width=15)
        stats_table.add_column("Percentual", style="yellow", width=15)

        total_events = len(recent_events)

        if not recent_events:
            stats_table.add_row("Total de Eventos", "0", "—")
            return CommandResult.success_result(stats_table, "rich")

        type_counts: Dict[str, int] = {}
        for event in recent_events:
            key = event.event_type.value
            type_counts[key] = type_counts.get(key, 0) + 1

        for event_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / total_events * 100) if total_events > 0 else 0
            stats_table.add_row(
                event_type.replace("_", " ").title(),
                str(count),
                f"{percentage:.1f}%",
            )

        return CommandResult.success_result(stats_table, "rich")

    async def _export_logs(self, filename: str, format_type: str) -> CommandResult:
        try:
            event_count = self.audit_logger.event_count()
            exported_file = self.audit_logger.export_audit_log(filename, format_type)

            return CommandResult.success_result(
                Panel(
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
                ),
                "rich",
            )

        except CommandError:
            raise
        except Exception as e:
            logger.error("Falha ao exportar logs: %s", e, exc_info=True)
            raise CommandError(f"Falha ao exportar logs: {str(e)}")

    async def _clear_logs(self) -> CommandResult:
        count = self.audit_logger.clear_events()

        return CommandResult.success_result(
            Panel(
                Text(
                    f"✅ **Logs em Memória Limpos**\n\n"
                    f"Removidos {count} eventos da memória.\n\n"
                    f"Nota: Os logs persistentes em {self.audit_logger.log_file} são preservados.\n"
                    f"Use ferramentas de sistema de arquivos para gerenciar o arquivo de log se necessário.",
                    style="yellow",
                ),
                title="🗑️ Logs Limpos",
                border_style="yellow",
            ),
            "rich",
        )

    def get_help(self) -> str:
        return """Visualizar logs de auditoria de segurança e eventos do sistema

Uso:
  /logs                       Visão geral dos logs e atividade recente
  /logs recent [N]            Mostrar N eventos mais recentes (padrão: 50)
  /logs security              Apenas eventos de segurança
  /logs permissions           Verificações de permissão e negativas
  /logs secrets               Eventos de detecção de segredos
  /logs tools                 Logs de execução de ferramentas
  /logs plans                 Logs de execução de planos
  /logs errors                Apenas erros e avisos
  /logs errors --severity warning|error|critical
                              Filtrar por nível de severidade
  /logs summary               Estatísticas detalhadas
  /logs export <arquivo> [fmt] Exportar logs para arquivo (json/csv)
  /logs clear                 Limpar logs da memória (mantém logs persistentes)

Tipos de Evento:
  • permission_check      - Validações de controle de acesso
  • permission_denied     - Tentativas de acesso bloqueadas
  • secret_detected       - Dados sensíveis encontrados em arquivos
  • secret_redacted       - Dados sensíveis sanitizados
  • tool_execution        - Eventos de execução de ferramentas
  • plan_execution        - Eventos de fluxo de planos
  • approval_required     - Aprovação manual necessária
  • approval_granted      - Aprovação manual concedida

Níveis de Severidade:
  • debug       - Informações de depuração
  • info        - Informações gerais
  • warning     - Possíveis problemas
  • error       - Erros e falhas
  • critical    - Eventos críticos de segurança

Formatos de Exportação:
  • json        - JSON Lines (padrão)
  • csv         - Valores separados por vírgula

Exemplos:
  /logs recent 100                    Mostrar últimos 100 eventos
  /logs security                      Mostrar eventos de segurança
  /logs permissions                   Mostrar eventos de controle de acesso
  /logs errors --severity critical    Apenas eventos críticos
  /logs export relatorio.json        Exportar para JSON

Arquivos de Log:
  • logs/security_audit.log          - Logs estruturados persistentes
  • Buffer em memória                - Eventos recentes para acesso rápido

Comandos Relacionados:
  • /permissions - Gerenciar regras de segurança
  • /tools - Listar ferramentas disponíveis
  • /status - Visão geral do sistema"""
