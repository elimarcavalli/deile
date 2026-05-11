"""Memory Command — connect to real MemoryManager; real checkpoint persistence."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import (export_timestamp, get_memory_manager, get_session,
                      split_args, success_panel)

_CHECKPOINT_DIR = Path.home() / ".deile" / "checkpoints"
_CHECKPOINT_INDEX = _CHECKPOINT_DIR / "index.json"


def _load_index() -> Dict[str, Any]:
    if _CHECKPOINT_INDEX.exists():
        try:
            return json.loads(_CHECKPOINT_INDEX.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_index(index: Dict[str, Any]) -> None:
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _checkpoint_path(name: str) -> Path:
    safe = Path(name).name  # strip any directory components
    if not safe or safe != name:
        raise CommandError(f"Nome de checkpoint inválido: '{name}'")
    return _CHECKPOINT_DIR / f"{safe}.json"


class MemoryCommand(DirectCommand):
    """Advanced memory and session state management with granular controls."""

    cli_flag = "--memory"
    cli_help = "Show memory subsystem status (working, episodic, semantic, procedural)."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="memory",
            description="Advanced memory and session state management with detailed controls.",
        )
        super().__init__(config)

    async def execute(self, context: CommandContext) -> CommandResult:
        try:
            parts = split_args(context)
            if not parts:
                return await self._show_memory_status(context)
            action = parts[0].lower()
            dispatch = {
                "status": lambda: self._show_memory_status(context),
                "clear": lambda: self._clear_memory_type(context, parts[1] if len(parts) > 1 else "conversation"),
                "usage": lambda: self._show_memory_usage(context),
                "export": lambda: self._export_memory_state(context, parts[1:]),
                "compact": lambda: self._compact_memory(context),
                "save": lambda: self._save_checkpoint(context, parts[1] if len(parts) > 1 else f"checkpoint_{int(__import__('time').time())}"),
                "restore": lambda: self._restore_checkpoint(context, parts[1]) if len(parts) >= 2 else self._err("restore requer nome do checkpoint"),
                "list": lambda: self._list_checkpoints(),
            }
            handler = dispatch.get(action)
            if not handler:
                raise CommandError(f"Ação desconhecida: {action}")
            return await handler()
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(f"Falha ao executar /memory: {exc}") from exc

    async def _err(self, msg: str) -> CommandResult:
        raise CommandError(msg)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def _show_memory_status(self, context: CommandContext) -> CommandResult:
        mm = get_memory_manager(context)

        real_usage: Optional[Dict[str, Any]] = None
        if mm is not None:
            try:
                real_usage = await mm.get_memory_usage()
            except Exception as exc:
                real_usage = {"error": str(exc)}

        table = Table(title="🧠 Status de Memória", show_header=False)
        table.add_column("Componente", style="bold cyan", width=22)
        table.add_column("Uso", style="green", width=15)
        table.add_column("Descrição", style="dim", width=30)

        if real_usage and "error" not in real_usage and real_usage.get("status") != "not_initialized":
            components = real_usage.get("components", {})
            for layer, stats in components.items():
                entries = stats.get("entries", stats.get("total_entries", 0))
                size_mb = stats.get("memory_mb", 0)
                label = layer.replace("_", " ").title()
                table.add_row(label, f"{entries} entradas", f"{size_mb:.3f} MB")
            total_mb = real_usage.get("total_memory_mb", 0)
            table.add_row("TOTAL", f"{total_mb:.3f} MB", "Uso total estimado")
        elif real_usage and "error" in real_usage:
            table.add_row("MemoryManager", f"[INDISPONÍVEL: {real_usage['error'][:40]}]", "")
        else:
            # Fallback to session-level data
            session = get_session(context)
            conv = len(getattr(session, "conversation_history", None) or []) if session else 0
            table.add_row("Conversa", str(conv), "Mensagens no histórico")

        # Active plans (always available via singleton)
        try:
            from ...orchestration.plan_manager import get_plan_manager
            pm = get_plan_manager()
            total_plans = len(await pm.list_plans())
            table.add_row("Planos Ativos", str(pm.active_plan_count()), f"Total: {total_plans}")
        except Exception:
            table.add_row("Planos", "[INDISPONÍVEL]", "")

        # Audit events
        try:
            from ...security.audit_logger import get_audit_logger
            table.add_row("Eventos de Auditoria", str(get_audit_logger().event_count()), "Buffer em memória")
        except Exception:
            pass

        management = Panel(
            Text(
                "/memory clear <tipo>         — limpar tipo de memória\n"
                "/memory compact              — otimizar memória\n"
                "/memory export [arquivo]     — exportar estado\n"
                "/memory save <nome>          — salvar checkpoint\n"
                "/memory restore <nome>       — restaurar checkpoint\n"
                "/memory list                 — listar checkpoints",
                style="dim",
            ),
            title="Opções de Gerenciamento",
            border_style="blue",
        )

        return CommandResult.success_result(Group(table, management), "rich")

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------

    async def _clear_memory_type(self, context: CommandContext, memory_type: str) -> CommandResult:
        cleared = 0
        desc = ""
        session = get_session(context)

        if memory_type in ("conversation", "conv", "history"):
            history = getattr(session, "conversation_history", None) if session else None
            if history is not None:
                cleared = len(history)
                history.clear()
            desc = "mensagens de conversa"

        elif memory_type in ("context", "ctx"):
            ctx_data = getattr(session, "context_data", None) if session else None
            if ctx_data is not None:
                cleared = len(ctx_data)
                ctx_data.clear()
            desc = "entradas de contexto"

        elif memory_type in ("memory", "mem", "buffer"):
            mem_buf = getattr(session, "memory", None) if session else None
            if mem_buf is not None:
                cleared = len(mem_buf)
                mem_buf.clear()
            desc = "buffer de memória longa"

        elif memory_type in ("plans", "plan"):
            try:
                from ...orchestration.plan_manager import get_plan_manager
                pm = get_plan_manager()
                for plan_id in pm.active_plan_ids():
                    await pm.stop_plan(plan_id)
                cleared = pm.clear_active_state()
                desc = "planos ativos"
            except Exception:
                desc = "planos (nenhum encontrado)"

        elif memory_type in ("audit", "logs"):
            try:
                from ...security.audit_logger import get_audit_logger
                cleared = get_audit_logger().clear_events()
                desc = "eventos de auditoria"
            except Exception:
                desc = "eventos de auditoria"

        elif memory_type in ("all", "everything"):
            total = 0
            for attr in ("conversation_history", "context_data", "memory"):
                buf = getattr(session, attr, None) if session else None
                if buf is not None:
                    total += len(buf)
                    buf.clear()
            if session:
                for attr in ("tokens", "cost"):
                    if hasattr(session, attr):
                        setattr(session, attr, 0 if attr == "tokens" else 0.0)
            try:
                from ...orchestration.plan_manager import get_plan_manager
                pm = get_plan_manager()
                for plan_id in pm.active_plan_ids():
                    await pm.stop_plan(plan_id)
                total += pm.clear_active_state()
            except Exception:
                pass
            try:
                from ...security.audit_logger import get_audit_logger
                total += get_audit_logger().clear_events()
            except Exception:
                pass
            cleared = total
            desc = "todos os componentes de memória"

        else:
            raise CommandError(
                f"Tipo de memória desconhecido: '{memory_type}'. "
                "Use: conversation, context, memory, plans, audit, all"
            )

        return CommandResult.success_result(
            Panel(
                Text(
                    f"✅ Memória limpa\n\nTipo: {memory_type}\n"
                    f"Itens removidos: {cleared} {desc}\n\n"
                    f"{'Otimização concluída!' if cleared > 0 else 'Nada a limpar.'}",
                    style="green",
                ),
                title="Memória Limpa",
                border_style="green",
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    async def _show_memory_usage(self, context: CommandContext) -> CommandResult:
        table = Table(title="🔍 Análise Detalhada de Uso de Memória", show_header=True, header_style="bold yellow")
        table.add_column("Componente", style="cyan", width=22)
        table.add_column("Contagem", style="green", width=10, justify="center")
        table.add_column("Tamanho Est.", style="blue", width=14, justify="center")
        table.add_column("Impacto", style="red", width=12)

        total_impact = 0
        session = get_session(context)
        if session:
            history = getattr(session, "conversation_history", None)
            if history:
                count = len(history)
                impact = "Alto" if count > 100 else "Médio" if count > 50 else "Baixo"
                table.add_row("Histórico de Conversa", str(count), f"{count * 200}B", impact)
                total_impact += 2 if count > 50 else 0

            ctx_data = getattr(session, "context_data", None)
            if ctx_data:
                count = len(ctx_data)
                impact = "Alto" if count > 50 else "Médio" if count > 20 else "Baixo"
                table.add_row("Dados de Contexto", str(count), f"{count * 500}B", impact)
                total_impact += 2 if count > 20 else 0

        try:
            from ...orchestration.plan_manager import get_plan_manager
            active = get_plan_manager().active_plan_count()
            if active > 0:
                impact = "Alto" if active > 5 else "Médio" if active > 2 else "Baixo"
                table.add_row("Planos Ativos", str(active), f"{active * 1000}B", impact)
                total_impact += 3 if active > 2 else 0
        except Exception:
            pass

        try:
            from ...security.audit_logger import get_audit_logger
            audit_count = get_audit_logger().event_count()
            if audit_count > 0:
                impact = "Médio" if audit_count > 500 else "Baixo"
                table.add_row("Eventos de Auditoria", str(audit_count), f"{audit_count * 300}B", impact)
                total_impact += 1 if audit_count > 500 else 0
        except Exception:
            pass

        if total_impact > 5:
            rec = "🔴 Alto Impacto — considere /memory clear all"
            rec_color = "red"
        elif total_impact > 2:
            rec = "🟡 Impacto Médio — considere /memory compact"
            rec_color = "yellow"
        else:
            rec = "🟢 Baixo Impacto — uso ótimo"
            rec_color = "green"

        rec_panel = Panel(Text(rec, style=rec_color), title="Recomendação", border_style=rec_color)
        return CommandResult.success_result(Group(table, rec_panel), "rich")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def _export_memory_state(self, context: CommandContext, args: list) -> CommandResult:
        output_path_str = args[0] if args else f"memory_export_{export_timestamp()}.json"
        output_path = Path(output_path_str)

        mm = get_memory_manager(context)
        usage: Dict[str, Any] = {}
        if mm is not None:
            try:
                usage = await mm.get_memory_usage()
            except Exception as exc:
                usage = {"error": str(exc)}

        export_data = {
            "exported_at": datetime.now().isoformat(),
            "memory_usage": usage,
        }

        try:
            await asyncio.to_thread(
                output_path.write_text,
                json.dumps(export_data, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            raise CommandError(f"Falha ao escrever arquivo de export: {exc}") from exc

        return CommandResult.success_result(
            Panel(
                Text(f"✅ Estado de memória exportado para:\n{output_path.resolve()}", style="green"),
                title="Export Concluído",
                border_style="green",
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # Compact
    # ------------------------------------------------------------------

    async def _compact_memory(self, context: CommandContext) -> CommandResult:
        mm = get_memory_manager(context)
        if mm is None:
            return CommandResult.success_result(
                Panel(
                    Text("[INDISPONÍVEL: MemoryManager não acessível — impossível compactar]", style="yellow"),
                    title="Compactação",
                    border_style="yellow",
                ),
                "rich",
            )

        try:
            report = await mm.optimize_memory(force=False)
        except Exception as exc:
            raise CommandError(f"Falha na compactação de memória: {exc}") from exc

        lines = ["✅ Compactação concluída\n"]
        for k, v in report.items():
            lines.append(f"  {k}: {v}")

        return CommandResult.success_result(
            success_panel("\n".join(lines), title="Compactação de Memória"),
            "rich",
        )

    # ------------------------------------------------------------------
    # Save checkpoint
    # ------------------------------------------------------------------

    async def _save_checkpoint(self, context: CommandContext, name: str) -> CommandResult:
        mm = get_memory_manager(context)
        usage: Dict[str, Any] = {}
        if mm is not None:
            try:
                usage = await mm.get_memory_usage()
            except Exception:
                pass

        checkpoint_data = {
            "name": name,
            "saved_at": datetime.now().isoformat(),
            "memory_usage": usage,
        }

        cp_path = _checkpoint_path(name)
        _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            cp_path.write_text,
            json.dumps(checkpoint_data, indent=2, default=str),
            encoding="utf-8",
        )

        index = _load_index()
        index[name] = {
            "name": name,
            "saved_at": checkpoint_data["saved_at"],
            "size_bytes": cp_path.stat().st_size,
        }
        _save_index(index)

        return CommandResult.success_result(
            Panel(
                Text(
                    f"✅ Checkpoint salvo\n\nNome: {name}\nArquivo: {cp_path}\n"
                    f"Timestamp: {checkpoint_data['saved_at']}\n\n"
                    f"Use '/memory restore {name}' para restaurar.",
                    style="green",
                ),
                title="Checkpoint Salvo",
                border_style="green",
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # Restore checkpoint
    # ------------------------------------------------------------------

    async def _restore_checkpoint(self, context: CommandContext, name: str) -> CommandResult:
        cp_path = _checkpoint_path(name)
        if not cp_path.exists():
            raise CommandError(
                f"Checkpoint '{name}' não encontrado em {_CHECKPOINT_DIR}. "
                "Use '/memory list' para ver checkpoints disponíveis."
            )

        try:
            checkpoint_data = json.loads(cp_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CommandError(f"Falha ao ler checkpoint '{name}': {exc}") from exc

        saved_at = checkpoint_data.get("saved_at", "desconhecido")

        return CommandResult.success_result(
            Panel(
                Text(
                    f"📂 Checkpoint '{name}' carregado\n\n"
                    f"Salvo em: {saved_at}\n\n"
                    f"Snapshot disponível para consulta.\n"
                    f"Nota: a reinjeção automática de estado de memória\n"
                    f"requer reinicialização do agente com este checkpoint.",
                    style="blue",
                ),
                title="Checkpoint Carregado",
                border_style="blue",
            ),
            "rich",
        )

    # ------------------------------------------------------------------
    # List checkpoints
    # ------------------------------------------------------------------

    async def _list_checkpoints(self) -> CommandResult:
        index = _load_index()
        if not index:
            return CommandResult.success_result(
                Panel(Text("Nenhum checkpoint salvo.", style="dim"), title="Checkpoints", border_style="dim"),
                "rich",
            )

        table = Table(title="💾 Checkpoints Disponíveis", show_header=True, header_style="bold cyan")
        table.add_column("Nome", style="cyan", width=25)
        table.add_column("Salvo em", style="dim", width=22)
        table.add_column("Tamanho", style="yellow", width=12)

        for name, meta in sorted(index.items(), key=lambda x: x[1].get("saved_at", "")):
            size_kb = meta.get("size_bytes", 0) / 1024
            table.add_row(name, meta.get("saved_at", "?")[:19], f"{size_kb:.1f} KB")

        return CommandResult.success_result(table, "rich")

    def get_help(self) -> str:
        return """Gerenciamento avançado de memória e estado de sessão

Uso:
  /memory                     Visão geral do status de memória
  /memory status              Status detalhado
  /memory usage               Análise de uso de memória
  /memory clear <tipo>        Limpar tipo específico de memória
  /memory compact             Otimizar memória sem perda de dados
  /memory export [arquivo]    Exportar estado de memória para JSON
  /memory save <nome>         Salvar checkpoint de memória
  /memory restore <nome>      Restaurar checkpoint de memória
  /memory list                Listar checkpoints disponíveis

Tipos para /memory clear:
  conversation, conv, history  — histórico de conversação
  context, ctx                 — dados de contexto
  memory, mem, buffer          — buffer de memória longa
  plans, plan                  — planos de orquestração ativos
  audit, logs                  — buffer do log de auditoria
  all, everything              — todos os componentes"""
