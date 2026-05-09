"""Status Command — live system status from real subsystem modules."""

from __future__ import annotations

import asyncio
import platform
import socket
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import psutil
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.__version__ import __version__

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand

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
        loop = asyncio.get_event_loop()
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


def _indisponivel(reason: str) -> str:
    return f"[INDISPONÍVEL: {reason}]"


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

    async def execute(self, context: CommandContext) -> CommandResult:
        self._emit_audit_event(context)
        args = context.args
        try:
            parts = args.strip().split() if args.strip() else []
            if not parts:
                return await self._show_complete_status(context)
            section = parts[0].lower()
            dispatch = {
                "system": self._show_system_status,
                "models": self._show_models_status,
                "tools": self._show_tools_status,
                "memory": self._show_memory_status,
                "plans": self._show_plans_status,
                "connectivity": self._show_connectivity_status,
                "performance": self._show_performance_status,
            }
            handler = dispatch.get(section)
            if not handler:
                raise CommandError(f"Seção desconhecida: {section}")
            return await handler(context)
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(f"Falha ao executar /status: {exc}") from exc

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _emit_audit_event(self, context: CommandContext) -> None:
        try:
            from ...security.audit_logger import (AuditEventType,
                                                  SeverityLevel,
                                                  get_audit_logger)
            get_audit_logger().log_event(
                event_type=AuditEventType.COMMAND_EXECUTED,
                severity=SeverityLevel.INFO,
                actor="user",
                resource="/status",
                action="execute",
                result="initiated",
                details={"args": context.args},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Complete overview
    # ------------------------------------------------------------------

    async def _show_complete_status(self, context: CommandContext) -> CommandResult:
        system_info = self._get_system_info()
        models_info = self._get_models_info()
        tools_info = self._get_tools_info()
        health_info = self._get_health_info()

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
    # System info (overview panel)
    # ------------------------------------------------------------------

    def _get_system_info(self) -> Dict[str, Any]:
        try:
            return {
                "deile_version": __version__,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "platform": platform.system(),
                "platform_release": platform.release(),
                "platform_version": platform.version(),
                "architecture": platform.machine(),
                "hostname": platform.node(),
                "uptime": self._get_system_uptime(),
                "cpu_count": psutil.cpu_count(),
                "memory_total": psutil.virtual_memory().total,
                "memory_used": psutil.virtual_memory().used,
                "memory_percent": psutil.virtual_memory().percent,
                "disk_usage": psutil.disk_usage(".").percent,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _get_system_uptime(self) -> str:
        try:
            boot_time = datetime.fromtimestamp(psutil.boot_time())
            delta = datetime.now() - boot_time
            h, rem = divmod(delta.seconds, 3600)
            m, _ = divmod(rem, 60)
            return f"{delta.days}d {h}h {m}m"
        except Exception:
            return "desconhecido"

    # ------------------------------------------------------------------
    # Models info (overview panel)
    # ------------------------------------------------------------------

    def _get_models_info(self) -> Dict[str, Any]:
        try:
            from ...core.models.router import get_model_router
            router = get_model_router()
            providers = list(router.providers.keys())
            active_key = providers[0] if providers else None
            active_provider = active_key.split(":", 1)[0] if active_key else _indisponivel("nenhum provedor")
            active_model = active_key.split(":", 1)[1] if active_key and ":" in active_key else (active_key or _indisponivel("nenhum modelo"))
            return {
                "active_model": active_model,
                "active_provider": active_provider,
                "total_providers": len(providers),
                "providers": providers,
            }
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Tools info (overview panel)
    # ------------------------------------------------------------------

    def _get_tools_info(self) -> Dict[str, Any]:
        try:
            from ...tools.registry import get_tool_registry
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

    # ------------------------------------------------------------------
    # Health info (overview panel)
    # ------------------------------------------------------------------

    def _get_health_info(self) -> Dict[str, Any]:
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
            status = "saudável" if health_score >= 80 else "atenção" if health_score >= 60 else "crítico"
            return {
                "overall_status": status,
                "health_score": health_score,
                "cpu_usage": cpu_percent,
                "memory_usage": memory.percent,
                "warnings": warnings,
                "uptime": self._get_system_uptime(),
                "last_check": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {"error": str(exc), "overall_status": "desconhecido"}

    # ------------------------------------------------------------------
    # Panel builders (overview)
    # ------------------------------------------------------------------

    def _create_system_panel(self, info: Dict[str, Any]) -> Panel:
        if "error" in info:
            return Panel(Text(f"Erro: {info['error']}", style="red"), title="🖥️ Sistema", border_style="red")
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
        return Panel(Text(content, style="green"), title="🖥️ Sistema", border_style="green")

    def _create_models_panel(self, info: Dict[str, Any]) -> Panel:
        if "error" in info:
            return Panel(Text(f"Erro: {info['error']}", style="red"), title="🤖 Modelos", border_style="red")
        content = (
            f"🟢 Modelo: {info.get('active_model', '?')}\n"
            f"🏢 Provedor: {info.get('active_provider', '?')}\n"
            f"📊 Provedores: {info.get('total_providers', 0)}"
        )
        return Panel(Text(content, style="cyan"), title="🤖 Modelos IA", border_style="cyan")

    def _create_tools_panel(self, info: Dict[str, Any]) -> Panel:
        if "error" in info:
            return Panel(Text(f"Erro: {info['error']}", style="red"), title="🔧 Tools", border_style="red")
        content = (
            f"🔧 Total: {info.get('total_tools', 0)}\n"
            f"✅ Habilitadas: {info.get('enabled_tools', 0)}\n"
            f"⛔ Desabilitadas: {info.get('disabled_tools', 0)}\n\n"
            f"📂 Categorias: {info.get('categories', 0)}\n"
            f"📋 Schemas: {info.get('tools_with_schemas', 0)}\n"
            f"🔄 Funções: {info.get('function_definitions', 0)}"
        )
        return Panel(Text(content, style="yellow"), title="🔧 Tools", border_style="yellow")

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
        info = self._get_system_info()
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
                providers_by_id = getattr(tier_router, "_providers_by_id", {})
                for pid, prov in providers_by_id.items():
                    model = getattr(prov, "model_name", "?")
                    table.add_row(f"{pid}:{model}", pid, str(model))
            else:
                for key, prov in providers.items():
                    parts = key.split(":", 1)
                    provider_id = parts[0]
                    model_id = parts[1] if len(parts) > 1 else "?"
                    table.add_row(key, provider_id, model_id)

            if table.row_count == 0:
                table.add_row(_indisponivel("nenhum provedor registrado"), "—", "—")

            return CommandResult.success_result(table, "rich")
        except Exception as exc:
            return CommandResult.success_result(
                Panel(Text(f"Erro ao obter informações de modelos: {exc}", style="red"), title="🤖 Modelos", border_style="red"),
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
                Panel(Text(f"Erro ao obter tools: {exc}", style="red"), title="🔧 Tools", border_style="red"),
                "rich",
            )

    # ------------------------------------------------------------------
    # /status memory
    # ------------------------------------------------------------------

    async def _show_memory_status(self, context: CommandContext) -> CommandResult:
        memory_manager = None
        if context.agent:
            memory_manager = getattr(context.agent, "memory_manager", None)

        if memory_manager is None:
            content = _indisponivel("MemoryManager não acessível neste contexto")
            return CommandResult.success_result(
                Panel(Text(content, style="yellow"), title="💾 Memória", border_style="yellow"), "rich"
            )

        try:
            usage = await memory_manager.get_memory_usage()
        except Exception as exc:
            usage = {"error": str(exc)}

        if "error" in usage:
            return CommandResult.success_result(
                Panel(Text(f"Erro: {usage['error']}", style="red"), title="💾 Memória", border_style="red"), "rich"
            )

        if usage.get("status") == "not_initialized":
            return CommandResult.success_result(
                Panel(Text(_indisponivel("MemoryManager não inicializado"), style="yellow"), title="💾 Memória", border_style="yellow"),
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
            active_plans = list(plan_manager._active_plans.values())
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
                Panel(Text(f"Erro ao obter planos: {exc}", style="red"), title="📋 Planos", border_style="red"), "rich"
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

        probe_tasks = {pid: _probe_host(host) for pid, host in hosts_to_probe.items()}
        results = await asyncio.gather(*probe_tasks.values(), return_exceptions=True)
        probe_results: Dict[str, Tuple[bool, float]] = {}
        for pid, result in zip(probe_tasks.keys(), results):
            if isinstance(result, Exception):
                probe_results[pid] = (False, 0.0)
            else:
                probe_results[pid] = result

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
        try:
            from ...storage.usage_repository import UsageRepository
            repo = UsageRepository()
            session_id = context.session_id if hasattr(context, "session_id") else "default"
            records = repo.records_for_session(session_id)
            total_tokens = sum(getattr(r, "total_tokens", 0) for r in records)
            total_cost = repo.cost_for_session(session_id)
            request_count = len(records)
        except Exception:
            total_tokens = 0
            total_cost = 0.0
            request_count = 0

        cpu_percent = psutil.cpu_percent(interval=0)
        memory = psutil.virtual_memory()

        table = Table(title="📊 Performance do Sistema", show_header=True, header_style="bold green")
        table.add_column("Métrica", style="cyan", width=25)
        table.add_column("Valor", style="white", width=20)

        table.add_row("CPU (%)", f"{cpu_percent:.1f}%")
        table.add_row("Memória usada (%)", f"{memory.percent:.1f}%")
        table.add_row("Memória disponível", f"{memory.available // (1024**2)} MB")
        table.add_row("Requisições na sessão", str(request_count))
        table.add_row("Tokens na sessão", str(total_tokens))
        table.add_row("Custo na sessão (USD)", f"${total_cost:.6f}")

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
