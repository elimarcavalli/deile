"""Permissions Command — manage security rules with real persistence."""

from __future__ import annotations

from datetime import datetime
from typing import List

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ...security.permissions import (PermissionLevel, PermissionRule,
                                     ResourceType, get_permission_manager)
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import split_args


def _persist(pm) -> None:
    """Save rules to disk if config_path is configured."""
    if pm.config_path:
        pm.config_path.parent.mkdir(parents=True, exist_ok=True)
        pm.save_rules_to_config(pm.config_path)


class PermissionsCommand(DirectCommand):
    """Manage security rules and permissions for tools and resources."""

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="permissions",
            description="Manage security rules and permissions for tools and resources.",
        )
        super().__init__(config)
        self.permission_manager = get_permission_manager()

    async def execute(self, context: CommandContext) -> CommandResult:
        try:
            parts = split_args(context)
            if not parts:
                return await self._show_permissions_overview()
            action = parts[0].lower()
            dispatch = {
                "list": lambda: self._list_rules(parts[1:]),
                "show": lambda: self._show_rule(parts[1]) if len(parts) >= 2 else self._err("show requer ID: /permissions show <id>"),
                "check": lambda: self._check_permission(parts[1], parts[2], parts[3]) if len(parts) >= 4 else self._err("check requer: /permissions check <tool> <resource> <action>"),
                "add": lambda: self._add_rule(parts[1:]) if len(parts) >= 6 else self._err("add requer: /permissions add <id> <nome> <tipo> <padrão> <nível> [tools]"),
                "enable": lambda: self._enable_rule(parts[1], True) if len(parts) >= 2 else self._err("enable requer ID"),
                "disable": lambda: self._enable_rule(parts[1], False) if len(parts) >= 2 else self._err("disable requer ID"),
                "remove": lambda: self._remove_rule(parts[1], "--confirm" in parts) if len(parts) >= 2 else self._err("remove requer ID"),
                "audit": lambda: self._show_audit_log(parts[1:]),
                "sandbox": lambda: self._manage_sandbox(parts[1]) if len(parts) >= 2 else self._err("sandbox requer: on|off|status"),
                "help": lambda: self._show_help(),
            }
            handler = dispatch.get(action)
            if not handler:
                raise CommandError(f"Ação desconhecida: {action}")
            return await handler()
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(f"Falha ao executar /permissions: {exc}") from exc

    async def _err(self, msg: str) -> CommandResult:
        raise CommandError(msg)

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    async def _show_permissions_overview(self) -> CommandResult:
        pm = self.permission_manager
        total = len(pm.rules)
        enabled = sum(1 for r in pm.rules if r.enabled)
        disabled = total - enabled

        type_counts: dict = {}
        for rule in pm.rules:
            rt = rule.resource_type.value
            type_counts[rt] = type_counts.get(rt, 0) + 1

        level_counts: dict = {}
        for rule in pm.rules:
            lv = rule.permission_level.value
            level_counts[lv] = level_counts.get(lv, 0) + 1

        overview = Table(title="🛡️ Visão Geral de Permissões", show_header=False)
        overview.add_column("Métrica", style="bold cyan", width=20)
        overview.add_column("Valor", style="green", width=15)
        overview.add_column("Detalhes", style="dim", width=30)
        overview.add_row("Total de Regras", str(total), "Regras de segurança ativas")
        overview.add_row("Habilitadas", str(enabled), "Aplicadas atualmente")
        overview.add_row("Desabilitadas", str(disabled), "Temporariamente inativas")
        overview.add_row("Nível Padrão", pm.default_permission.value, "Permissão de fallback")
        sandbox_label = "🟢 Ativo" if pm.sandbox_enabled else "🔴 Inativo"
        overview.add_row("Sandbox", sandbox_label, "Modo de isolamento")

        types_table = Table(title="📁 Tipos de Recurso Protegidos", show_header=True, header_style="bold yellow")
        types_table.add_column("Tipo", style="cyan")
        types_table.add_column("Regras", style="green", justify="center")
        for res_type, count in sorted(type_counts.items()):
            types_table.add_row(res_type.title(), str(count))

        levels_table = Table(title="🔐 Níveis de Permissão", show_header=True, header_style="bold red")
        levels_table.add_column("Nível", style="red")
        levels_table.add_column("Regras", style="green", justify="center")
        for level, count in sorted(level_counts.items()):
            levels_table.add_row(level.title(), str(count))

        usage = Panel(
            Text(
                "/permissions list [filtro]           — listar regras\n"
                "/permissions show <id>              — detalhes da regra\n"
                "/permissions add <id> <nome> <tipo> <padrão> <nível> [tools]\n"
                "/permissions enable/disable <id>    — ativar/desativar\n"
                "/permissions remove <id> [--confirm]\n"
                "/permissions audit [limite]         — log de auditoria\n"
                "/permissions sandbox on|off|status\n"
                "/permissions help",
                style="dim",
            ),
            title="Referência",
            border_style="blue",
        )

        return CommandResult.success_result(Group(overview, "", types_table, "", levels_table, "", usage), "rich")

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def _list_rules(self, filters: List[str]) -> CommandResult:
        rules = list(self.permission_manager.rules)
        filter_type = filters[0] if filters else None
        if filter_type:
            if filter_type in [rt.value for rt in ResourceType]:
                rules = [r for r in rules if r.resource_type.value == filter_type]
            elif filter_type in [pl.value for pl in PermissionLevel]:
                rules = [r for r in rules if r.permission_level.value == filter_type]
            else:
                rules = [r for r in rules if filter_type.lower() in r.id.lower() or filter_type.lower() in r.name.lower()]

        if not rules:
            return CommandResult.success_result(
                Panel(Text(f"Nenhuma regra encontrada{(' (filtro: ' + filter_type + ')') if filter_type else ''}.", style="yellow"),
                      title="🔍 Sem Resultados", border_style="yellow"),
                "rich",
            )

        table = Table(
            title=f"🛡️ Regras de Permissão ({len(rules)} encontradas)",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("ID", style="cyan", width=15)
        table.add_column("Nome", style="white", width=20)
        table.add_column("Tipo", style="yellow", width=10)
        table.add_column("Nível", style="red", width=10)
        table.add_column("Status", style="blue", width=8)
        table.add_column("Prioridade", style="magenta", width=10)

        for rule in sorted(rules, key=lambda r: r.priority):
            status = "✅ On" if rule.enabled else "❌ Off"
            rule_id = rule.id[:13] + "…" if len(rule.id) > 13 else rule.id
            name = rule.name[:18] + "…" if len(rule.name) > 18 else rule.name
            table.add_row(rule_id, name, rule.resource_type.value, rule.permission_level.value, status, str(rule.priority))

        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # Show
    # ------------------------------------------------------------------

    async def _show_rule(self, rule_id: str) -> CommandResult:
        rule = self.permission_manager.get_rule_by_id(rule_id)
        if not rule:
            raise CommandError(f"Regra '{rule_id}' não encontrada")

        table = Table(title=f"🛡️ Regra: {rule.name}", show_header=False)
        table.add_column("Propriedade", style="bold cyan", width=18)
        table.add_column("Valor", style="white", width=40)
        table.add_row("ID", rule.id)
        table.add_row("Nome", rule.name)
        table.add_row("Descrição", rule.description)
        table.add_row("Tipo de Recurso", rule.resource_type.value)
        table.add_row("Padrão", rule.resource_pattern)
        table.add_row("Nível de Permissão", rule.permission_level.value)
        table.add_row("Prioridade", str(rule.priority))
        table.add_row("Status", "✅ Habilitada" if rule.enabled else "❌ Desabilitada")
        tools_text = "* (todas)" if "*" in rule.tool_names else ", ".join(rule.tool_names)
        table.add_row("Tools", tools_text)
        if rule.conditions:
            table.add_row("Condições", str(rule.conditions))

        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    async def _check_permission(self, tool: str, resource: str, action: str) -> CommandResult:
        allowed = self.permission_manager.check_permission(tool, resource, action)
        applicable = [
            r for r in sorted(self.permission_manager.rules, key=lambda r: r.priority)
            if r.enabled and r.applies_to_tool(tool) and r.matches_resource(resource)
        ]
        applied_rule = applicable[0] if applicable else None

        icon = "✅" if allowed else "❌"
        color = "green" if allowed else "red"
        result_text = "PERMITIDO" if allowed else "NEGADO"

        table = Table(title=f"{icon} Verificação de Permissão — {result_text}", show_header=False)
        table.add_column("Propriedade", style="bold cyan", width=15)
        table.add_column("Valor", style=color, width=35)
        table.add_row("Tool", tool)
        table.add_row("Recurso", resource)
        table.add_row("Ação", action)
        table.add_row("Resultado", f"{icon} {result_text}")
        if applied_rule:
            table.add_row("Regra Aplicada", applied_rule.id)
            table.add_row("Nível", applied_rule.permission_level.value)
        else:
            table.add_row("Regra Aplicada", "Padrão")
            table.add_row("Nível Padrão", self.permission_manager.default_permission.value)

        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------

    async def _add_rule(self, args: List[str]) -> CommandResult:
        rule_id = args[0]
        name = args[1]
        resource_type_str = args[2]
        pattern = args[3]
        level_str = args[4]
        tool_names = args[5].split(",") if len(args) > 5 else ["*"]

        try:
            resource_type = ResourceType(resource_type_str)
        except ValueError:
            valid = [rt.value for rt in ResourceType]
            raise CommandError(f"Tipo de recurso inválido '{resource_type_str}'. Válidos: {valid}") from None

        try:
            permission_level = PermissionLevel(level_str)
        except ValueError:
            valid = [pl.value for pl in PermissionLevel]
            raise CommandError(f"Nível de permissão inválido '{level_str}'. Válidos: {valid}") from None

        if self.permission_manager.get_rule_by_id(rule_id) is not None:
            raise CommandError(f"Regra com ID '{rule_id}' já existe. Use outro ID.")

        rule = PermissionRule(
            id=rule_id,
            name=name,
            description=f"Regra criada via /permissions add em {datetime.now().isoformat()}",
            resource_type=resource_type,
            resource_pattern=pattern,
            tool_names=tool_names,
            permission_level=permission_level,
            priority=100,
        )
        self.permission_manager.add_rule(rule)
        _persist(self.permission_manager)
        self._emit_audit_event("add", rule_id, f"Regra adicionada: {name}")

        return CommandResult.success_result(
            Panel(
                Text(
                    f"✅ Regra criada com sucesso\n\n"
                    f"ID: {rule_id}\nNome: {name}\n"
                    f"Tipo: {resource_type.value}\nNível: {permission_level.value}\n"
                    f"Tools: {', '.join(tool_names)}",
                    style="green",
                ),
                title="Regra Adicionada",
                border_style="green",
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    async def _enable_rule(self, rule_id: str, enabled: bool) -> CommandResult:
        rule = self.permission_manager.get_rule_by_id(rule_id)
        if not rule:
            raise CommandError(f"Regra '{rule_id}' não encontrada")

        if rule.enabled == enabled:
            state = "habilitada" if enabled else "desabilitada"
            return CommandResult.success_result(
                Panel(Text(f"Regra '{rule_id}' já está {state}.", style="dim"), title="Sem Alteração", border_style="dim"),
                "rich",
            )

        rule.enabled = enabled
        _persist(self.permission_manager)
        action_str = "habilitada" if enabled else "desabilitada"
        self._emit_audit_event("enable" if enabled else "disable", rule_id, f"Regra {action_str}")

        color = "green" if enabled else "red"
        icon = "✅" if enabled else "❌"
        return CommandResult.success_result(
            Panel(
                Text(f"{icon} Regra '{rule_id}' {action_str} com sucesso.\nNome: {rule.name}", style=color),
                title=f"Regra {action_str.title()}",
                border_style=color,
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    async def _remove_rule(self, rule_id: str, confirmed: bool) -> CommandResult:
        rule = self.permission_manager.get_rule_by_id(rule_id)
        if not rule:
            raise CommandError(f"Regra '{rule_id}' não encontrada")

        if not confirmed:
            return CommandResult.success_result(
                Panel(
                    Text(
                        f"⚠️  Você está prestes a remover permanentemente a regra:\n\n"
                        f"  ID: {rule_id}\n  Nome: {rule.name}\n\n"
                        f"Para confirmar, execute:\n"
                        f"  /permissions remove {rule_id} --confirm",
                        style="yellow",
                    ),
                    title="Confirmação Necessária",
                    border_style="yellow",
                ),
                "rich",
            )

        removed = self.permission_manager.remove_rule(rule_id)
        if not removed:
            raise CommandError(f"Falha ao remover regra '{rule_id}'")

        _persist(self.permission_manager)
        self._emit_audit_event("remove", rule_id, f"Regra removida: {rule.name}")

        return CommandResult.success_result(
            Panel(
                Text(f"✅ Regra '{rule_id}' ({rule.name}) removida com sucesso.", style="green"),
                title="Regra Removida",
                border_style="green",
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def _show_audit_log(self, args: List[str]) -> CommandResult:
        try:
            from ...security.audit_logger import (AuditEventType,
                                                  get_audit_logger)
            limit = int(args[0]) if args and args[0].isdigit() else 50
            audit_logger = get_audit_logger()
            events = audit_logger.get_recent_events(
                event_type=AuditEventType.SECURITY_POLICY_CHANGED,
                limit=limit,
            )
            if not events:
                events = audit_logger.get_recent_events(limit=limit)
            events = [
                e for e in events
                if e.event_type in (
                    AuditEventType.SECURITY_POLICY_CHANGED,
                    AuditEventType.PERMISSION_CHECK,
                    AuditEventType.PERMISSION_DENIED,
                )
            ]
        except Exception as exc:
            return CommandResult.success_result(
                Panel(Text(f"Erro ao ler log de auditoria: {exc}", style="red"), title="🔍 Auditoria", border_style="red"),
                "rich",
            )

        if not events:
            return CommandResult.success_result(
                Panel(Text("Nenhum evento de permissão registrado ainda.", style="dim"), title="🔍 Auditoria", border_style="dim"),
                "rich",
            )

        table = Table(title=f"🔍 Log de Auditoria de Permissões ({len(events)} eventos)", show_header=True, header_style="bold cyan")
        table.add_column("Timestamp", style="dim", width=20)
        table.add_column("Tipo", style="cyan", width=22)
        table.add_column("Actor", style="yellow", width=15)
        table.add_column("Recurso", style="white", width=20)
        table.add_column("Resultado", style="green", width=12)

        for event in events[-limit:]:
            ts = event.timestamp.strftime("%Y-%m-%d %H:%M:%S") if hasattr(event.timestamp, "strftime") else str(event.timestamp)[:19]
            table.add_row(ts, event.event_type.value, event.actor, event.resource[:18], event.result)

        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # Sandbox
    # ------------------------------------------------------------------

    async def _manage_sandbox(self, mode: str) -> CommandResult:
        pm = self.permission_manager
        mode_lower = mode.lower()

        if mode_lower == "status":
            state = "ATIVO" if pm.sandbox_enabled else "INATIVO"
            icon = "🟢" if pm.sandbox_enabled else "🔴"
            return CommandResult.success_result(
                Panel(Text(f"{icon} Sandbox: {state}", style="green" if pm.sandbox_enabled else "red"),
                      title="Status do Sandbox", border_style="dim"),
                "rich",
            )

        if mode_lower in ("on", "enable"):
            pm.sandbox_enabled = True
            _persist(pm)
            self._emit_audit_event("sandbox_on", "sandbox", "Sandbox ativado")
            return CommandResult.success_result(
                Panel(Text("✅ Sandbox ativado.", style="green"), title="Sandbox", border_style="green"),
                "rich",
            )

        if mode_lower in ("off", "disable"):
            pm.sandbox_enabled = False
            _persist(pm)
            self._emit_audit_event("sandbox_off", "sandbox", "Sandbox desativado")
            return CommandResult.success_result(
                Panel(Text("⚠️  Sandbox desativado.", style="yellow"), title="Sandbox", border_style="yellow"),
                "rich",
            )

        raise CommandError(f"Modo inválido: '{mode}'. Use: on, off, status")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    async def _show_help(self) -> CommandResult:
        help_text = (
            "/permissions                           — visão geral\n"
            "/permissions list [filtro]             — listar regras\n"
            "/permissions show <id>                — detalhes da regra\n"
            "/permissions check <tool> <res> <ação> — verificar permissão\n"
            "/permissions add <id> <nome> <tipo> <padrão> <nível> [tools]\n"
            "/permissions enable <id>              — habilitar regra\n"
            "/permissions disable <id>             — desabilitar regra\n"
            "/permissions remove <id> [--confirm]  — remover regra\n"
            "/permissions audit [limite]           — log de auditoria\n"
            "/permissions sandbox on|off|status    — modo sandbox\n"
            "\nTipos de recurso: file, directory, command, network, system\n"
            "Níveis de permissão: none, read, write, execute, admin"
        )
        return CommandResult.success_result(
            Panel(Text(help_text, style="dim"), title="📖 Ajuda — /permissions", border_style="blue"),
            "rich",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_audit_event(self, action: str, resource: str, details_msg: str) -> None:
        try:
            from ...security.audit_logger import (AuditEventType,
                                                  SeverityLevel,
                                                  get_audit_logger)
            get_audit_logger().log_event(
                event_type=AuditEventType.SECURITY_POLICY_CHANGED,
                severity=SeverityLevel.WARNING,
                actor="user",
                resource=resource,
                action=action,
                result="success",
                details={"message": details_msg},
            )
        except Exception:
            pass

    def get_help(self) -> str:
        return "Gerenciar regras de segurança e permissões para tools e recursos."
