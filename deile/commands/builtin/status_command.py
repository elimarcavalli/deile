"""Status Command — live system status from real subsystem modules."""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from typing import Any, Dict, Tuple

from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import (emit_audit_event, error_panel, get_memory_manager,
                      indisponivel, split_args, success_panel, warning_panel,
                      wrap_command_errors)
from ._status_collectors import (collect_health_info, collect_models_info,
                                 collect_performance_info, collect_system_info,
                                 collect_tools_info, collect_usage_summary)

_PROVIDER_HOSTS: Dict[str, str] = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "google": "generativelanguage.googleapis.com",
    "deepseek": "api.deepseek.com",
    "gemini": "generativelanguage.googleapis.com",
}


async def _probe_host(host: str, port: int = 443, timeout: float = 5.0) -> Tuple[bool, float]:
    start = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        conn = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: socket.create_connection((host, port), timeout=timeout)
            ),
            timeout=timeout,
        )
        conn.close()
        return True, (time.monotonic() - start) * 1000
    except Exception:
        return False, (time.monotonic() - start) * 1000


class StatusCommand(DirectCommand):
    """Complete system status — all data from real subsystem modules."""

    cli_flag = "--status"
    cli_help = "Show DEILE system status overview and exit."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="status",
            description="Complete system status, health monitoring and connectivity information.",
        )
        super().__init__(config)

    _DISPATCH = {
        "system": "_show_system_status",
        "models": "_show_models_status",
        "tools": "_show_tools_status",
        "memory": "_show_memory_status",
        "plans": "_show_plans_status",
        "connectivity": "_show_connectivity_status",
        "performance": "_show_performance_status",
    }

    @wrap_command_errors("status", message_template="Falha ao executar /{name}: {exc}")
    async def execute(self, context: CommandContext) -> CommandResult:
        self._emit_audit_event(context)
        parts = split_args(context)
        if not parts:
            return await self._show_complete_status(context)
        section = parts[0].lower()
        method_name = self._DISPATCH.get(section)
        if not method_name:
            raise CommandError(f"Seção desconhecida: {section}")
        return await getattr(self, method_name)(context)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _emit_audit_event(self, context: CommandContext) -> None:
        from ...security.audit_logger import AuditEventType, SeverityLevel
        emit_audit_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=SeverityLevel.INFO,
            resource="/status",
            action="execute",
            details={"args": context.args},
        )

    # ------------------------------------------------------------------
    # Complete overview
    # ------------------------------------------------------------------

    async def _show_complete_status(self, context: CommandContext) -> CommandResult:
        system_info = collect_system_info()
        models_info = collect_models_info()
        tools_info = collect_tools_info()
        health_info = collect_health_info()

        left_column = Columns([self._create_system_panel(system_info), self._create_tools_panel(tools_info)], equal=True)
        right_column = Columns([self._create_models_panel(models_info), self._create_health_panel(health_info)], equal=True)

        usage_panel = Panel(
            Text(
                "Vistas detalhadas:\n"
                "• /status system        — informações do sistema\n"
                "• /status models        — modelos e provedores de IA\n"
                "• /status tools         — registro de tools\n"
                "• /status memory        — memória e estado da sessão\n"
                "• /status plans         — planos ativos\n"
                "• /status connectivity  — conectividade de rede\n"
                "• /status performance   — métricas de performance",
                style="dim",
            ),
            title="📋 Seções de Status",
            border_style="dim",
        )

        return CommandResult.success_result(Group(left_column, right_column, usage_panel), "rich")

    # ------------------------------------------------------------------
    # Panel builders (overview) — collectors live in _status_collectors
    # ------------------------------------------------------------------

    def _create_system_panel(self, info: Dict[str, Any]) -> Panel:
        if "error" in info:
            return error_panel(f"Erro: {info['error']}", title="🖥️ Sistema")
        content = (
            f"💻 DEILE v{info.get('deile_version', '?')}\n\n"
            f"🐍 Python: {info.get('python_version', '?')}\n"
            f"🖥️  SO: {info.get('platform', '?')} {info.get('platform_release', '')}\n"
            f"🏗️  Arch: {info.get('architecture', '?')}\n"
            f"🌐 Host: {info.get('hostname', '?')}\n\n"
            f"⏱️  Uptime: {info.get('uptime', '?')}\n"
            f"🧮 CPUs: {info.get('cpu_count', '?')}\n"
            f"💾 Memória: {info.get('memory_percent', 0):.1f}% usada\n"
            f"💿 Disco: {info.get('disk_usage', 0):.1f}% usado"
        )
        return success_panel(content, title="🖥️ Sistema")

    def _create_models_panel(self, info: Dict[str, Any]) -> Panel:
        if "error" in info:
            return error_panel(f"Erro: {info['error']}", title="🤖 Modelos")
        content = (
            f"🟢 Modelo: {info.get('active_model', '?')}\n"
            f"🏢 Provedor: {info.get('active_provider', '?')}\n"
            f"📊 Provedores: {info.get('total_providers', 0)}"
        )
        return Panel(Text(content, style="cyan"), title="🤖 Modelos IA", border_style="cyan")

    def _create_tools_panel(self, info: Dict[str, Any]) -> Panel:
        if "error" in info:
            return error_panel(f"Erro: {info['error']}", title="🔧 Tools")
        content = (
            f"🔧 Total: {info.get('total_tools', 0)}\n"
            f"✅ Habilitadas: {info.get('enabled_tools', 0)}\n"
            f"⛔ Desabilitadas: {info.get('disabled_tools', 0)}\n\n"
            f"📂 Categorias: {info.get('categories', 0)}\n"
            f"📋 Schemas: {info.get('tools_with_schemas', 0)}\n"
            f"🔄 Funções: {info.get('function_definitions', 0)}"
        )
        return warning_panel(content, title="🔧 Tools")

    def _create_health_panel(self, info: Dict[str, Any]) -> Panel:
        status = info.get("overall_status", "desconhecido")
        color_map = {"saudável": ("🟢", "green"), "atenção": ("🟡", "yellow"), "crítico": ("🔴", "red")}
        icon, color = color_map.get(status, ("⚪", "dim"))
        content = (
            f"{icon} Status: {status.title()}\n"
            f"📊 Score: {info.get('health_score', 0)}/100\n\n"
            f"💻 CPU: {info.get('cpu_usage', 0):.1f}%\n"
            f"💾 Memória: {info.get('memory_usage', 0):.1f}%\n"
            f"⏱️  Uptime: {info.get('uptime', '?')}"
        )
        if info.get("warnings"):
            content += "\n\n⚠️ Avisos:\n" + "".join(f"  • {w}\n" for w in info["warnings"])
        else:
            content += "\n\n✨ Todos os sistemas normais"
        return Panel(Text(content, style=color), title="🩺 Saúde", border_style=color)

    # ------------------------------------------------------------------
    # /status system
    # ------------------------------------------------------------------

    async def _show_system_status(self, context: CommandContext) -> CommandResult:
        info = collect_system_info()
        table = Table(title="💻 Informações Detalhadas do Sistema", show_header=True, header_style="bold green")
        table.add_column("Componente", style="cyan", width=20)
        table.add_column("Valor", style="white", width=30)
        table.add_column("Detalhes", style="dim", width=25)
        if "error" not in info:
            table.add_row("Versão DEILE", info["deile_version"], "Versão atual")
            table.add_row("Python", info["python_version"], sys.executable)
            table.add_row("SO", f"{info['platform']} {info['platform_release']}", info["platform_version"][:30])
            table.add_row("Arquitetura", info["architecture"], "")
            table.add_row("Hostname", info["hostname"], "")
            table.add_row("Uptime", info["uptime"], "")
            table.add_row("CPUs", str(info["cpu_count"]), "")
            table.add_row("Memória Total", f"{info['memory_total'] // (1024**3):.1f} GB", f"{info['memory_percent']:.1f}% usada")
            table.add_row("Disco", f"{info['disk_usage']:.1f}%", "Diretório atual")
        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # /status models
    # ------------------------------------------------------------------

    async def _show_models_status(self, context: CommandContext) -> CommandResult:
        try:
            from ...core.models.router import get_model_router
            from ...core.models.tier_router import get_tier_router
            router = get_model_router()
            tier_router = get_tier_router()

            table = Table(title="🤖 Provedores de IA Registrados", show_header=True, header_style="bold cyan")
            table.add_column("Chave", style="cyan", width=35)
            table.add_column("Provedor", style="white", width=15)
            table.add_column("Modelo", style="green", width=25)

            providers = router.providers
            if not providers:
                for pid, prov in getattr(tier_router, "_providers_by_id", {}).items():
                    model = getattr(prov, "model_name", "?")
                    table.add_row(f"{pid}:{model}", pid, str(model))
            else:
                for key in providers:
                    parts = key.split(":", 1)
                    table.add_row(key, parts[0], parts[1] if len(parts) > 1 else "?")

            if table.row_count == 0:
                table.add_row(indisponivel("nenhum provedor registrado"), "—", "—")

            return CommandResult.success_result(table, "rich")
        except Exception as exc:
            return CommandResult.success_result(
                error_panel(f"Erro ao obter informações de modelos: {exc}", title="🤖 Modelos"),
                "rich",
            )

    # ------------------------------------------------------------------
    # /status tools
    # ------------------------------------------------------------------

    async def _show_tools_status(self, context: CommandContext) -> CommandResult:
        try:
            from ...tools.registry import get_tool_registry
            registry = get_tool_registry()
            stats = registry.get_stats()
            enabled_names = {t.name for t in registry.list_enabled()}

            table = Table(
                title=f"🔧 Tools Registradas ({stats['total_tools']} total)",
                show_header=True,
                header_style="bold yellow",
            )
            table.add_column("Nome", style="cyan", width=25)
            table.add_column("Categoria", style="yellow", width=15)
            table.add_column("Status", style="green", width=10)

            for tool in sorted(registry.list_all(), key=lambda t: t.name):
                status_icon = "✅" if tool.name in enabled_names else "❌"
                table.add_row(tool.name, getattr(tool, "category", "?"), status_icon)

            summary = Panel(
                Text(
                    f"Total: {stats['total_tools']}  |  "
                    f"Habilitadas: {stats['enabled_tools']}  |  "
                    f"Categorias: {stats['categories']}",
                    style="dim",
                ),
                border_style="dim",
            )
            return CommandResult.success_result(Group(table, summary), "rich")
        except Exception as exc:
            return CommandResult.success_result(
                error_panel(f"Erro ao obter tools: {exc}", title="🔧 Tools"),
                "rich",
            )

    # ------------------------------------------------------------------
    # /status memory
    # ------------------------------------------------------------------

    async def _show_memory_status(self, context: CommandContext) -> CommandResult:
        memory_manager = get_memory_manager(context)

        if memory_manager is None:
            content = indisponivel("MemoryManager não acessível neste contexto")
            return CommandResult.success_result(
                warning_panel(content, title="💾 Memória"), "rich"
            )

        try:
            usage = await memory_manager.get_memory_usage()
        except Exception as exc:
            usage = {"error": str(exc)}

        if "error" in usage:
            return CommandResult.success_result(
                error_panel(f"Erro: {usage['error']}", title="💾 Memória"), "rich"
            )

        if usage.get("status") == "not_initialized":
            return CommandResult.success_result(
                warning_panel(indisponivel("MemoryManager não inicializado"), title="💾 Memória"),
                "rich",
            )

        table = Table(title="💾 Uso de Memória por Camada", show_header=True, header_style="bold blue")
        table.add_column("Camada", style="cyan", width=25)
        table.add_column("Entradas", style="green", width=12, justify="right")
        table.add_column("Tamanho (MB)", style="yellow", width=15, justify="right")

        components = usage.get("components", {})
        for layer_name, layer_stats in components.items():
            entries = layer_stats.get("entries", layer_stats.get("total_entries", "?"))
            size_mb = layer_stats.get("memory_mb", 0)
            table.add_row(layer_name.replace("_", " ").title(), str(entries), f"{size_mb:.3f}")

        total_mb = usage.get("total_memory_mb", 0)
        summary = Panel(Text(f"Total estimado: {total_mb:.3f} MB", style="dim"), border_style="dim")
        return CommandResult.success_result(Group(table, summary), "rich")

    # ------------------------------------------------------------------
    # /status plans
    # ------------------------------------------------------------------

    async def _show_plans_status(self, context: CommandContext) -> CommandResult:
        try:
            from ...orchestration.plan_manager import get_plan_manager
            plan_manager = get_plan_manager()
            active_plans = plan_manager.iter_active_plans()
            all_plans = await plan_manager.list_plans()

            table = Table(
                title=f"📋 Planos ({len(active_plans)} ativos / {len(all_plans)} total)",
                show_header=True,
                header_style="bold magenta",
            )
            table.add_column("ID", style="cyan", width=20)
            table.add_column("Título", style="white", width=25)
            table.add_column("Status", style="yellow", width=12)
            table.add_column("Passos", style="green", width=15)

            for plan_data in all_plans:
                plan_id = plan_data.get("id", "?")[:18]
                title = plan_data.get("title", "?")[:23]
                status = plan_data.get("status", "?")
                total = plan_data.get("total_steps", 0)
                done = plan_data.get("completed_steps", 0)
                is_active = any(p.id == plan_data.get("id") for p in active_plans)
                status_display = f"{'🔄 ' if is_active else ''}{status}"
                table.add_row(plan_id, title, status_display, f"{done}/{total}")

            if not all_plans:
                table.add_row("—", "Nenhum plano", "—", "—")

            return CommandResult.success_result(table, "rich")
        except Exception as exc:
            return CommandResult.success_result(
                error_panel(f"Erro ao obter planos: {exc}", title="📋 Planos"), "rich"
            )

    # ------------------------------------------------------------------
    # /status connectivity
    # ------------------------------------------------------------------

    async def _show_connectivity_status(self, context: CommandContext) -> CommandResult:
        try:
            from ...core.models.router import get_model_router
            router = get_model_router()
            provider_ids = {key.split(":", 1)[0] for key in router.providers.keys()}
        except Exception:
            provider_ids = set()

        if not provider_ids:
            provider_ids = set(_PROVIDER_HOSTS.keys())

        hosts_to_probe: Dict[str, str] = {}
        for pid in provider_ids:
            host = _PROVIDER_HOSTS.get(pid)
            if host:
                hosts_to_probe[pid] = host

        if not hosts_to_probe:
            hosts_to_probe = dict(_PROVIDER_HOSTS)

        pids = list(hosts_to_probe.keys())
        raw = await asyncio.gather(
            *(_probe_host(hosts_to_probe[p]) for p in pids), return_exceptions=True
        )
        probe_results: Dict[str, Tuple[bool, float]] = {
            pid: (False, 0.0) if isinstance(r, Exception) else r
            for pid, r in zip(pids, raw)
        }

        table = Table(title="🌐 Conectividade com Provedores", show_header=True, header_style="bold cyan")
        table.add_column("Provedor", style="cyan", width=20)
        table.add_column("Host", style="dim", width=40)
        table.add_column("Status", style="green", width=12)
        table.add_column("Latência (ms)", style="yellow", width=15, justify="right")

        for pid, (ok, latency) in sorted(probe_results.items()):
            host = hosts_to_probe.get(pid, "?")
            status_icon = "🟢 OK" if ok else "🔴 FALHOU"
            latency_str = f"{latency:.0f}" if ok else "—"
            table.add_row(pid, host, status_icon, latency_str)

        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # /status performance
    # ------------------------------------------------------------------

    async def _show_performance_status(self, context: CommandContext) -> CommandResult:
        session_id = context.session_id if hasattr(context, "session_id") else "default"
        perf_info = collect_performance_info()
        usage_info = collect_usage_summary(session_id)

        table = Table(title="📊 Performance do Sistema", show_header=True, header_style="bold green")
        table.add_column("Métrica", style="cyan", width=25)
        table.add_column("Valor", style="white", width=20)

        table.add_row("CPU (%)", f"{perf_info.get('cpu_percent', 0):.1f}%")
        table.add_row("Memória usada (%)", f"{perf_info.get('memory_percent', 0):.1f}%")
        table.add_row("Memória disponível", f"{perf_info.get('memory_available_mb', 0)} MB")
        table.add_row("Requisições na sessão", str(usage_info.get("request_count", 0)))
        table.add_row("Tokens na sessão", str(usage_info.get("total_tokens", 0)))
        table.add_row("Custo na sessão (USD)", f"${usage_info.get('total_cost', 0.0):.6f}")

        return CommandResult.success_result(table, "rich")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def get_help(self) -> str:
        return """Status completo do sistema com dados ao vivo

Uso:
  /status                     Visão geral completa
  /status system              Informações detalhadas do sistema
  /status models              Modelos e provedores de IA
  /status tools               Registro de tools
  /status memory              Uso de memória por camada
  /status plans               Planos ativos e orquestração
  /status connectivity        Conectividade de rede e APIs
  /status performance         Métricas de performance

Indicadores de Saúde:
  🟢 Saudável — todos os sistemas normais
  🟡 Atenção   — problemas detectados
  🔴 Crítico   — atenção imediata necessária"""
