"""Context Command — exibe informações reais do contexto LLM"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from rich.columns import Columns
from rich.panel import Panel
from rich.text import Text

from ...core.exceptions import CommandError
from ..base import CommandResult, DirectCommand


def _indisponivel(motivo: str = "") -> Dict[str, Any]:
    return {"status": "indisponível", "motivo": motivo}


def _est_tokens(char_count: int) -> int:
    return max(1, char_count // 4)


class ContextCommand(DirectCommand):
    """Exibe contexto LLM completo: instruções, memória, histórico, tools e tokens"""

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="context",
            description="Exibe o contexto LLM completo: instruções, memória, histórico, tools e uso de tokens.",
        )
        super().__init__(config)

    async def execute(self, context) -> CommandResult:
        args = context.args if hasattr(context, "args") else ""

        try:
            parts = args.strip().split() if args.strip() else []
            format_type = "summary"
            export_format: Optional[str] = None
            show_tokens = False

            i = 0
            while i < len(parts):
                p = parts[i]
                if p in ("--format", "-f") and i + 1 < len(parts):
                    format_type = parts[i + 1]
                    i += 2
                elif p in ("--export", "-e") and i + 1 < len(parts):
                    export_format = parts[i + 1]
                    i += 2
                elif p in ("--export", "-e"):
                    export_format = "json"
                    i += 1
                elif p in ("--show-tokens", "-t"):
                    show_tokens = True
                    i += 1
                elif p in ("summary", "detailed", "json"):
                    format_type = p
                    i += 1
                else:
                    i += 1

            if format_type not in ("summary", "detailed", "json"):
                raise CommandError("Formato deve ser um de: summary, detailed, json")

            context_data = await self._get_context_data(context)

            if export_format:
                return await self._do_export(context_data, export_format)

            if format_type == "json":
                return CommandResult.success_result(
                    content=json.dumps(context_data, indent=2, default=str),
                    content_type="json",
                    command_name="context",
                    format="json",
                )

            if format_type == "summary":
                display = self._create_summary_display(context_data, show_tokens)
            else:
                display = self._create_detailed_display(context_data, show_tokens)

            return CommandResult.success_result(
                content=display,
                content_type="rich",
                command_name="context",
                format=format_type,
            )

        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"Falha ao exibir contexto: {exc}") from exc

    async def _get_context_data(self, context) -> Dict[str, Any]:
        agent = getattr(context, "agent", None)
        session = getattr(context, "session", None)

        # Fan out the two independent async subsystems in parallel
        mm = getattr(agent, "memory_manager", None) if agent else None
        ctx_mgr = getattr(agent, "context_manager", None) if agent else None
        raw_memory, raw_stats = await asyncio.gather(
            mm.get_memory_usage() if mm else asyncio.sleep(0),
            ctx_mgr.get_stats() if ctx_mgr else asyncio.sleep(0),
            return_exceptions=True,
        )

        # --- Persona ---
        persona_data: Dict[str, Any]
        try:
            pm = getattr(agent, "persona_manager", None) if agent else None
            persona = pm.get_active_persona() if pm else None
            if persona:
                persona_data = {
                    "name": getattr(persona, "name", "desconhecida"),
                    "active": True,
                }
            else:
                persona_data = {"name": "nenhuma", "active": False}
        except Exception as exc:
            persona_data = _indisponivel(str(exc))

        # --- Memória ---
        memory_data: Dict[str, Any]
        if not mm:
            memory_data = _indisponivel("memory_manager não disponível")
        elif isinstance(raw_memory, Exception):
            memory_data = _indisponivel(str(raw_memory))
        else:
            memory_data = raw_memory

        # --- Histórico de conversa ---
        conv_data: Dict[str, Any]
        try:
            history = getattr(session, "conversation_history", []) if session else []
            created_at = getattr(session, "created_at", None) if session else None
            last_activity = getattr(session, "last_activity", None) if session else None

            oldest = (
                datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
                if created_at
                else None
            )
            newest = (
                datetime.fromtimestamp(last_activity, tz=timezone.utc).isoformat()
                if last_activity
                else None
            )
            conv_data = {
                "messages": len(history),
                "oldest_message": oldest,
                "newest_message": newest,
            }
        except Exception as exc:
            conv_data = _indisponivel(str(exc))

        # --- Tools ---
        tools_data: Dict[str, Any]
        try:
            tr = getattr(agent, "tool_registry", None) if agent else None
            if tr:
                all_tools = tr.list_all()
                enabled_tools = tr.list_enabled()
                categories = list({getattr(t, "category", "other") for t in all_tools})
                tools_data = {
                    "total": len(all_tools),
                    "enabled": len(enabled_tools),
                    "categories": categories,
                }
            else:
                tools_data = _indisponivel("tool_registry não disponível")
        except Exception as exc:
            tools_data = _indisponivel(str(exc))

        # --- Modelo ---
        model_data: Dict[str, Any]
        try:
            mr = getattr(agent, "model_router", None) if agent else None
            if mr:
                providers = getattr(mr, "providers", {})
                provider_names = list(providers.keys())
                model_data = {
                    "providers": provider_names,
                    "strategy": getattr(mr, "strategy", _indisponivel("strategy não disponível")),
                }
            else:
                model_data = _indisponivel("model_router não disponível")
        except Exception as exc:
            model_data = _indisponivel(str(exc))

        # --- Sessão ---
        session_data: Dict[str, Any]
        try:
            if session:
                session_id = getattr(session, "session_id", "desconhecido")
                created_at = getattr(session, "created_at", None)
                session_data = {
                    "id": session_id,
                    "started": (
                        datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
                        if created_at
                        else None
                    ),
                }
            else:
                session_data = _indisponivel("sessão não disponível")
        except Exception as exc:
            session_data = _indisponivel(str(exc))

        # --- Instruções do sistema (estimativa) ---
        instructions_data: Dict[str, Any]
        if not ctx_mgr:
            instructions_data = _indisponivel("context_manager não disponível")
        elif isinstance(raw_stats, Exception):
            instructions_data = _indisponivel(str(raw_stats))
        else:
            instr_len = raw_stats.get("system_instructions_length")
            if instr_len is not None:
                instructions_data = {
                    "length": instr_len,
                    "tokens": _est_tokens(instr_len),
                    "token_count_method": "estimated",
                }
            else:
                # context_manager.get_stats() exposes context window limit, not instruction length
                instructions_data = {
                    "max_context_tokens": raw_stats.get("max_context_tokens", "indisponível"),
                    "context_builds": raw_stats.get("context_builds", 0),
                    "token_count_method": "not_available",
                }

        return {
            "system_instructions": instructions_data,
            "persona": persona_data,
            "memory": memory_data,
            "conversation_history": conv_data,
            "tools": tools_data,
            "model": model_data,
            "session": session_data,
        }

    async def _do_export(self, context_data: Dict[str, Any], fmt: str) -> CommandResult:
        if fmt not in ("json", "md"):
            return CommandResult.error_result(
                f"Formato de export inválido: '{fmt}'. Use 'json' ou 'md'."
            )

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"context_export_{ts}.{fmt}"

        try:
            if fmt == "json":
                content = json.dumps(context_data, indent=2, default=str)
                Path(fname).write_text(content, encoding="utf-8")
            else:
                lines = ["# Exportação de Contexto DEILE", ""]
                session = context_data.get("session", {})
                lines.append(f"**Sessão:** {session.get('id', 'indisponível')}")
                lines.append(f"**Iniciado:** {session.get('started', 'indisponível')}")
                lines.append("")

                persona = context_data.get("persona", {})
                lines.append(f"## Persona\n- **Nome:** {persona.get('name', 'indisponível')}")
                lines.append("")

                model = context_data.get("model", {})
                providers = model.get("providers", "indisponível")
                lines.append(f"## Modelo\n- **Provedores:** {providers}")
                lines.append("")

                tools = context_data.get("tools", {})
                lines.append(
                    f"## Tools\n- **Total:** {tools.get('total', 'indisponível')}"
                    f"\n- **Habilitadas:** {tools.get('enabled', 'indisponível')}"
                )
                lines.append("")

                conv = context_data.get("conversation_history", {})
                lines.append(
                    f"## Histórico\n- **Mensagens:** {conv.get('messages', 'indisponível')}"
                )
                content = "\n".join(lines)
                Path(fname).write_text(content, encoding="utf-8")

            return CommandResult.success_result(
                content=Panel(
                    Text(f"✅ Exportado: {fname}", style="green"),
                    title="📤 Export de Contexto",
                    border_style="green",
                ),
                content_type="rich",
                command_name="context",
                exported_file=fname,
            )
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao exportar: {exc}", error=exc)

    def _create_summary_display(self, data: Dict[str, Any], show_tokens: bool) -> Panel:
        persona = data.get("persona", {})
        conv = data.get("conversation_history", {})
        tools = data.get("tools", {})
        model = data.get("model", {})
        session = data.get("session", {})

        providers = model.get("providers", ["indisponível"])
        model_str = ", ".join(providers) if isinstance(providers, list) else str(providers)

        lines = [
            "📊 **Visão Geral do Contexto**",
            "",
            f"🤖 **Modelo/Provedores**: {model_str}",
            f"👤 **Persona**: {persona.get('name', 'indisponível')}",
            f"🆔 **Sessão**: {session.get('id', 'indisponível')}",
            f"💬 **Mensagens**: {conv.get('messages', 'indisponível')}",
            f"🔧 **Tools**: {tools.get('enabled', '?')}/{tools.get('total', '?')} habilitadas",
        ]

        if show_tokens:
            instr = data.get("system_instructions", {})
            lines.extend([
                "",
                "🎯 **Tokens (estimativa)**:",
                f"   • Instruções: {instr.get('tokens', 'indisponível')}",
                f"   • Método: {instr.get('token_count_method', 'indisponível')}",
            ])

        return Panel(
            Text("\n".join(lines), style="white"),
            title="🧠 Contexto LLM",
            border_style="blue",
            padding=(1, 2),
        )

    def _create_detailed_display(self, data: Dict[str, Any], show_tokens: bool) -> Columns:
        panels = []

        model = data.get("model", {})
        instr = data.get("system_instructions", {})
        providers = model.get("providers", ["indisponível"])
        model_str = ", ".join(providers) if isinstance(providers, list) else str(providers)

        model_content = [
            f"**Provedores**: {model_str}",
            f"**Estratégia**: {model.get('strategy', 'indisponível')}",
            "",
            "**Instruções do Sistema**:",
            f"  Tamanho: {instr.get('length', instr.get('max_context_tokens', 'indisponível'))} chars",
        ]
        if show_tokens:
            model_content.append(f"  Tokens: {instr.get('tokens', instr.get('token_count_method', 'indisponível'))}")
        panels.append(Panel("\n".join(model_content), title="🤖 Modelo", border_style="green"))

        memory = data.get("memory", {})
        if memory.get("status") == "indisponível":
            mem_content = [f"**Status**: {memory.get('motivo', 'indisponível')}"]
        else:
            mem_total = memory.get("total_memory_mb", "indisponível")
            components = memory.get("components", {})
            mem_content = [f"**Total**: {mem_total} MB", ""]
            for layer, stats in components.items():
                entries = stats.get("total_entries", stats.get("count", "?"))
                mem_content.append(f"  {layer}: {entries} entradas")
        panels.append(Panel("\n".join(mem_content), title="🧠 Memória", border_style="yellow"))

        tools = data.get("tools", {})
        cats = tools.get("categories", [])
        tools_content = [
            f"**Total**: {tools.get('total', 'indisponível')}",
            f"**Habilitadas**: {tools.get('enabled', 'indisponível')}",
            f"**Categorias**: {', '.join(cats) if cats else 'indisponível'}",
        ]
        panels.append(Panel("\n".join(tools_content), title="🔧 Tools", border_style="cyan"))

        conv = data.get("conversation_history", {})
        persona = data.get("persona", {})
        session = data.get("session", {})
        session_content = [
            f"**ID**: {session.get('id', 'indisponível')}",
            f"**Iniciado**: {session.get('started', 'indisponível')}",
            "",
            f"**Persona**: {persona.get('name', 'indisponível')}",
            f"**Mensagens**: {conv.get('messages', 'indisponível')}",
            f"**Última msg**: {conv.get('newest_message', 'indisponível')}",
        ]
        panels.append(Panel("\n".join(session_content), title="📋 Sessão", border_style="magenta"))

        return Columns(panels, equal=True, expand=True)

    def get_help(self) -> str:
        return """Exibe informações do contexto LLM

Uso:
  /context [formato] [opções]

Formatos:
  summary   Visão resumida (padrão)
  detailed  Detalhamento por componente
  json      Exportar como JSON no terminal

Opções:
  --show-tokens, -t         Exibe estimativa de tokens
  --export json|md, -e      Exporta contexto para arquivo
  --format FORMATO, -f      Especifica o formato de saída

Exemplos:
  /context                         Resumo
  /context detailed -t             Detalhado com tokens
  /context --export json           Exporta contexto para JSON"""
