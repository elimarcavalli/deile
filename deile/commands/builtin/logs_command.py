"""Logs Command — Visualização de logs de auditoria de segurança e eventos do sistema."""

import logging
from pathlib import Path
from typing import Dict, List

from rich.console import Group

from ...core.exceptions import CommandError
from ...security.audit_logger import (AuditEvent, AuditEventType,
                                      SeverityLevel, get_audit_logger)
from ..base import CommandContext, CommandResult, DirectCommand
from . import _logs_views
from ._shared import split_args, wrap_command_errors

logger = logging.getLogger(__name__)

MAX_SAFE_LIMIT = 500

_APPROVAL_TYPES = (
    AuditEventType.APPROVAL_REQUIRED,
    AuditEventType.APPROVAL_GRANTED,
    AuditEventType.APPROVAL_DENIED,
)

_SEVERITY_FILTER_MAP: Dict[str, List[SeverityLevel]] = {
    "warning": [SeverityLevel.WARNING],
    "error": [SeverityLevel.ERROR],
    "critical": [SeverityLevel.CRITICAL],
}

_VALID_EXPORT_FORMATS = {"json", "csv"}


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

    @wrap_command_errors("logs", message_template="Falha ao executar /{name}: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
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

    async def _show_logs_overview(self) -> CommandResult:
        summary = self.audit_logger.get_security_summary()
        if not isinstance(summary, dict):
            summary = {}
        recent_events = self.audit_logger.get_recent_events(20)

        content = _logs_views.build_overview_group(
            summary=summary, recent_events=recent_events
        )
        return CommandResult.success_result(content, "rich")

    async def _show_recent_logs(self, limit: int) -> CommandResult:
        effective_limit = min(limit, MAX_SAFE_LIMIT)
        capped = limit > MAX_SAFE_LIMIT

        events = self.audit_logger.get_recent_events(effective_limit)

        renderables: List = []
        if capped:
            renderables.append(_logs_views.build_recent_cap_warning(limit, MAX_SAFE_LIMIT))

        if not events:
            renderables.append(_logs_views.build_recent_empty_panel())
            return CommandResult.success_result(Group(*renderables), "rich")

        renderables.append(_logs_views.build_recent_logs_table(events))
        return CommandResult.success_result(Group(*renderables), "rich")

    async def _show_security_logs(self, _filters: List[str]) -> CommandResult:
        security_event_types = [
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SECRET_DETECTED,
            AuditEventType.SECRET_REDACTED,
            AuditEventType.SUSPICIOUS_ACTIVITY,
        ]

        all_events: List[AuditEvent] = []
        for event_type in security_event_types:
            all_events.extend(
                self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=event_type)
            )
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not all_events:
            return CommandResult.success_result(_logs_views.build_security_empty_panel(), "rich")

        return CommandResult.success_result(
            _logs_views.build_security_table(all_events), "rich"
        )

    async def _show_permission_logs(self, _filters: List[str]) -> CommandResult:
        permission_events = self.audit_logger.get_recent_events(
            MAX_SAFE_LIMIT, event_type=AuditEventType.PERMISSION_CHECK
        )
        denied_events = self.audit_logger.get_recent_events(
            MAX_SAFE_LIMIT, event_type=AuditEventType.PERMISSION_DENIED
        )

        all_events = permission_events + denied_events
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not all_events:
            return CommandResult.success_result(_logs_views.build_permission_empty_panel(), "rich")

        content = _logs_views.build_permission_group(
            permission_events=permission_events,
            denied_events=denied_events,
            combined_events=all_events,
        )
        return CommandResult.success_result(content, "rich")

    async def _show_secret_logs(self, _filters: List[str]) -> CommandResult:
        secret_events: List[AuditEvent] = []
        for event_type in (AuditEventType.SECRET_DETECTED, AuditEventType.SECRET_REDACTED):
            secret_events.extend(
                self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=event_type)
            )

        if not secret_events:
            return CommandResult.success_result(_logs_views.build_secret_empty_panel(), "rich")

        return CommandResult.success_result(
            _logs_views.build_secret_logs_table(secret_events), "rich"
        )

    async def _show_tool_logs(self, _filters: List[str]) -> CommandResult:
        tool_events = self.audit_logger.get_recent_events(
            MAX_SAFE_LIMIT, event_type=AuditEventType.TOOL_EXECUTION
        )

        if not tool_events:
            return CommandResult.success_result(_logs_views.build_tool_empty_panel(), "rich")

        return CommandResult.success_result(
            _logs_views.build_tool_logs_table(tool_events), "rich"
        )

    async def _show_plan_logs(self, _filters: List[str]) -> CommandResult:
        plan_events = self.audit_logger.get_recent_events(
            MAX_SAFE_LIMIT, event_type=AuditEventType.PLAN_EXECUTION
        )
        approval_events: List[AuditEvent] = [
            e
            for et in _APPROVAL_TYPES
            for e in self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, event_type=et)
        ]

        all_events = plan_events + approval_events
        all_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not all_events:
            return CommandResult.success_result(_logs_views.build_plan_empty_panel(), "rich")

        return CommandResult.success_result(
            _logs_views.build_plan_logs_table(all_events), "rich"
        )

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
            error_events.extend(
                self.audit_logger.get_recent_events(MAX_SAFE_LIMIT, severity=severity)
            )
        error_events.sort(key=lambda e: e.timestamp, reverse=True)

        if not error_events:
            return CommandResult.success_result(_logs_views.build_errors_empty_panel(), "rich")

        return CommandResult.success_result(
            _logs_views.build_errors_table(error_events), "rich"
        )

    async def _show_summary(self) -> CommandResult:
        recent_events = self.audit_logger.get_recent_events(MAX_SAFE_LIMIT)
        return CommandResult.success_result(
            _logs_views.build_summary_table(recent_events), "rich"
        )

    async def _export_logs(self, filename: str, format_type: str) -> CommandResult:
        try:
            event_count = self.audit_logger.event_count()
            exported_file = self.audit_logger.export_audit_log(filename, format_type)

            return CommandResult.success_result(
                _logs_views.build_export_success_panel(
                    exported_file=exported_file,
                    format_type=format_type,
                    event_count=event_count,
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
            _logs_views.build_clear_panel(count=count, log_file=str(self.audit_logger.log_file)),
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
