"""Comando /export — exporta histórico de conversa e dados da sessão"""

import asyncio
import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.panel import Panel
from rich.text import Text

from deile.__version__ import __version__

from ...core.exceptions import CommandError
from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import (
    ArgSpec,
    export_timestamp,
    get_agent,
    get_session,
    get_session_id,
    parse_flag_args,
    promote_positional_format,
    split_args,
)

logger = logging.getLogger(__name__)


class ExportCommand(DirectCommand):
    """Exporta histórico de conversa, planos e dados da sessão em vários formatos"""

    cli_flag = "--export"
    cli_takes_arg = True
    cli_arg_metavar = "CAMINHO"
    cli_help = "Exporta dados da sessão para CAMINHO (repassa args ao /export)."
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="export",
                description="Exporta histórico de conversa, planos e dados da sessão em vários formatos.",
            )
        )

    async def execute(self, context: CommandContext) -> CommandResult:
        try:
            parts = split_args(context)
            flags, positionals = parse_flag_args(
                parts,
                [
                    ArgSpec(("--format", "-f"), takes_value=True, dest="format"),
                    ArgSpec(("--path", "-p"), takes_value=True, dest="path"),
                    ArgSpec(("--no-artifacts",), dest="no_artifacts"),
                    ArgSpec(("--no-plans",), dest="no_plans"),
                    ArgSpec(("--no-session",), dest="no_session"),
                ],
                strict=True,
            )
            format_type = flags.get("format", "md")
            export_path = flags.get("path")
            include_artifacts = not flags.get("no_artifacts")
            include_plans = not flags.get("no_plans")
            include_session = not flags.get("no_session")
            # Positionals: first known format word promotes to format (only if still
            # default); otherwise it sets export_path (last wins, matching prior).
            format_type, leftover_positionals = promote_positional_format(
                positionals,
                format_type,
                "md",
                ("txt", "md", "json", "zip"),
            )
            for token in leftover_positionals:
                export_path = token

            if format_type not in ("txt", "md", "json", "zip"):
                raise CommandError("Formato deve ser um de: txt, md, json, zip")

            if not export_path:
                export_path = f"./EXPORTS/deile_export_{export_timestamp()}"

            panel = await self._perform_export(
                format_type,
                export_path,
                include_artifacts,
                include_plans,
                include_session,
                context,
            )
            return CommandResult.success_result(panel, "rich")

        except CommandError:
            raise
        except Exception as exc:
            return CommandResult.error_result(
                f"Falha ao exportar dados: {exc}", error=exc
            )

    async def _perform_export(
        self,
        format_type: str,
        export_path: str,
        include_artifacts: bool,
        include_plans: bool,
        include_session: bool,
        context: Optional[Any],
    ) -> Panel:
        export_data = await self._get_export_data(
            context, include_artifacts, include_plans, include_session
        )
        export_dir = Path(export_path)
        export_dir.mkdir(parents=True, exist_ok=True)

        if format_type == "zip":
            exported_files = [
                str(await self._create_zip_export(export_data, export_dir))
            ]
        else:
            exported_files = await self._create_individual_exports(
                export_data, export_dir, format_type
            )

        return self._create_export_summary(exported_files, export_data, format_type)

    async def _get_export_data(
        self,
        context: Optional[Any],
        include_artifacts: bool,
        include_plans: bool,
        include_session: bool,
    ) -> Dict[str, Any]:
        agent = get_agent(context)
        session = get_session(context)

        # --- Sessão e histórico ---
        session_id = get_session_id(context)
        history: List[Dict[str, Any]] = (
            getattr(session, "conversation_history", []) if session else []
        )
        created_at = getattr(session, "created_at", None) if session else None

        messages: List[Dict[str, Any]] = []
        for idx, msg in enumerate(history):
            messages.append(
                {
                    "id": idx + 1,
                    "role": msg.get("role", "unknown"),
                    "content": msg.get("content", ""),
                    "timestamp": msg.get("timestamp", None),
                }
            )

        data: Dict[str, Any] = {
            "export_metadata": {
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "deile_version": __version__,
                "session_id": session_id or "indisponível",
                "format_version": "2.0",
                "data_sources": ["AgentSession"],
            },
            "conversation": {
                "session_id": session_id or "indisponível",
                "started": (
                    datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
                    if created_at
                    else "indisponível"
                ),
                "total_messages": len(messages),
                "messages": messages,
            },
        }

        if include_session:
            # Modelo ativo
            model_name = "indisponível"
            try:
                mr = getattr(agent, "model_router", None)
                if mr:
                    providers = getattr(mr, "providers", {})
                    model_name = (
                        ", ".join(providers.keys()) if providers else "indisponível"
                    )
            except (
                Exception
            ) as exc:  # model_router é best-effort — falha não aborta o export
                logger.debug("export: falha ao ler model_router: %s", exc)

            # Persona ativa
            persona_name = "indisponível"
            try:
                pm = getattr(agent, "persona_manager", None)
                if pm:
                    persona = pm.get_active_persona()
                    if persona:
                        persona_name = getattr(persona, "name", "indisponível")
            except (
                Exception
            ) as exc:  # persona_manager é best-effort — falha não aborta o export
                logger.debug("export: falha ao ler persona_manager: %s", exc)

            # Memória
            memory_stats: Dict[str, Any] = {"status": "indisponível"}
            try:
                mm = getattr(agent, "memory_manager", None)
                if mm:
                    memory_stats = await mm.get_memory_usage()
            except (
                Exception
            ) as exc:  # memory_manager é best-effort — falha não aborta o export
                logger.debug("export: falha ao ler memory_manager: %s", exc)

            data["session_info"] = {
                "model": model_name,
                "persona": {"name": persona_name},
                "memory": memory_stats,
            }
            data["export_metadata"]["data_sources"].append("PersonaManager")
            data["export_metadata"]["data_sources"].append("MemoryManager")

        if include_artifacts:
            artifacts: List[Dict[str, Any]] = []
            try:
                artifacts_dir = Path("ARTIFACTS")
                if artifacts_dir.exists():
                    for run_dir in sorted(artifacts_dir.iterdir()):
                        if run_dir.is_dir():
                            for af in run_dir.glob("*.json"):
                                artifacts.append(
                                    {"path": str(af), "size": af.stat().st_size}
                                )
                data["export_metadata"]["data_sources"].append("ArtifactManager")
            except (
                Exception
            ) as exc:  # leitura de artifacts é best-effort — falha não aborta o export
                logger.debug("export: falha ao coletar artifacts: %s", exc)
            data["artifacts"] = {
                "count": len(artifacts),
                "items": artifacts,
                "note": "nenhum artifact nesta sessão" if not artifacts else "",
            }

        if include_plans:
            plans: List[Dict[str, Any]] = []
            try:
                from deile.orchestration.plan_manager import get_plan_manager

                pm_inst = get_plan_manager()
                raw_plans = await pm_inst.list_plans()
                plans = raw_plans if raw_plans else []
                data["export_metadata"]["data_sources"].append("PlanManager")
            except (
                Exception
            ) as exc:  # plan_manager é best-effort — falha não aborta o export
                logger.debug("export: falha ao coletar planos: %s", exc)
            data["plans"] = {
                "count": len(plans),
                "items": plans,
                "note": "nenhum plano nesta sessão" if not plans else "",
            }

        return data

    async def _create_individual_exports(
        self, data: Dict[str, Any], export_dir: Path, format_type: str
    ) -> List[str]:
        exported = []

        if format_type == "json":
            jf = export_dir / "deile_export_complete.json"
            await asyncio.to_thread(
                jf.write_text, json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
            exported.append(str(jf))
        else:
            conv_file = export_dir / f"conversation.{format_type}"
            await asyncio.to_thread(
                conv_file.write_text,
                self._format_conversation(data.get("conversation", {}), format_type),
                encoding="utf-8",
            )
            exported.append(str(conv_file))

            if "session_info" in data:
                sf = export_dir / f"session_info.{format_type}"
                await asyncio.to_thread(
                    sf.write_text,
                    self._format_session(data["session_info"], format_type),
                    encoding="utf-8",
                )
                exported.append(str(sf))

            if "artifacts" in data and data["artifacts"].get("count", 0) > 0:
                af = export_dir / f"artifacts_manifest.{format_type}"
                await asyncio.to_thread(
                    af.write_text,
                    self._format_artifacts(data["artifacts"], format_type),
                    encoding="utf-8",
                )
                exported.append(str(af))

            if "plans" in data and data["plans"].get("count", 0) > 0:
                pf = export_dir / f"plans_manifest.{format_type}"
                await asyncio.to_thread(
                    pf.write_text,
                    self._format_plans(data["plans"], format_type),
                    encoding="utf-8",
                )
                exported.append(str(pf))

        return exported

    async def _create_zip_export(self, data: Dict[str, Any], export_dir: Path) -> Path:
        zip_path = export_dir / "deile_complete_export.zip"

        # Pre-render all string contents on the event loop, then perform the
        # blocking zipfile I/O in a single threaded call.
        complete_json = json.dumps(data, indent=2, default=str)
        conversation_md = self._format_conversation(data.get("conversation", {}), "md")
        session_md = (
            self._format_session(data["session_info"], "md")
            if "session_info" in data
            else None
        )
        manifest_json = json.dumps(
            {
                "generated_at": data["export_metadata"]["generated_at"],
                "deile_version": __version__,
                "session_id": data["export_metadata"]["session_id"],
                "data_sources": data["export_metadata"].get("data_sources", []),
            },
            indent=2,
            default=str,
        )

        def _write_zip() -> None:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("data/complete_export.json", complete_json)
                zf.writestr("conversation.md", conversation_md)
                if session_md is not None:
                    zf.writestr("session_info.md", session_md)
                zf.writestr("MANIFEST.json", manifest_json)

        await asyncio.to_thread(_write_zip)
        return zip_path

    def _format_conversation(self, conv: Dict[str, Any], fmt: str) -> str:
        session_id = conv.get("session_id", "indisponível")
        started = conv.get("started", "indisponível")
        total = conv.get("total_messages", 0)
        messages = conv.get("messages", [])

        if fmt == "md":
            lines = [
                "# Exportação de Conversa DEILE",
                "",
                f"**ID da Sessão:** {session_id}",
                f"**Início:** {started}",
                f"**Total de Mensagens:** {total}",
                "",
                "## Mensagens",
                "",
            ]
            for msg in messages:
                lines += [
                    f"### Mensagem {msg.get('id', '')} — {msg.get('role', '').title()}",
                    f"**Momento:** {msg.get('timestamp', 'desconhecido')}",
                    "",
                    str(msg.get("content", "")),
                    "",
                ]
        else:
            lines = [
                "EXPORTAÇÃO DE CONVERSA DEILE",
                "=" * 50,
                "",
                f"ID da Sessão: {session_id}",
                f"Início: {started}",
                f"Total de Mensagens: {total}",
                "",
                "MENSAGENS:",
                "-" * 20,
                "",
            ]
            for msg in messages:
                lines += [
                    f"[{msg.get('timestamp', '')}] {msg.get('role', '').upper()}:",
                    str(msg.get("content", "")),
                    "",
                ]

        return "\n".join(lines)

    def _format_session(self, session_info: Dict[str, Any], fmt: str) -> str:
        model = session_info.get("model", "indisponível")
        persona = session_info.get("persona", {}).get("name", "indisponível")
        memory = session_info.get("memory", {})
        total_mb = (
            memory.get("total_memory_mb", "indisponível")
            if isinstance(memory, dict)
            else "indisponível"
        )

        if fmt == "md":
            return (
                "# Informações da Sessão\n\n"
                f"## Modelo\n- **Provedores:** {model}\n\n"
                f"## Persona\n- **Nome:** {persona}\n\n"
                f"## Memória\n- **Total:** {total_mb} MB\n"
            )
        return (
            "INFORMAÇÕES DA SESSÃO\n"
            "====================\n\n"
            f"Provedores: {model}\n"
            f"Persona: {persona}\n"
            f"Memória Total: {total_mb} MB\n"
        )

    def _format_artifacts(self, artifacts: Dict[str, Any], fmt: str) -> str:
        count = artifacts.get("count", 0)
        items = artifacts.get("items", [])
        header = (
            "# Manifesto de Artifacts\n\n"
            if fmt == "md"
            else "MANIFESTO DE ARTIFACTS\n=====================\n\n"
        )
        content = header + f"Total: {count}\n\n"
        for af in items:
            path = af.get("path", "—")
            size = af.get("size", 0)
            if fmt == "md":
                content += f"- **{path}** ({size} bytes)\n"
            else:
                content += f"Arquivo: {path} ({size} bytes)\n"
        return content

    def _format_plans(self, plans: Dict[str, Any], fmt: str) -> str:
        count = plans.get("count", 0)
        items = plans.get("items", [])
        header = (
            "# Manifesto de Planos\n\n"
            if fmt == "md"
            else "MANIFESTO DE PLANOS\n===================\n\n"
        )
        content = header + f"Total: {count}\n\n"
        for plan in items:
            name = plan.get("name", plan.get("id", "—"))
            status = plan.get("status", "—")
            if fmt == "md":
                content += f"- **{name}** (status: {status})\n"
            else:
                content += f"Plano: {name} (status: {status})\n"
        return content

    def _create_export_summary(
        self, exported_files: List[str], export_data: Dict[str, Any], format_type: str
    ) -> Panel:
        metadata = export_data.get("export_metadata", {})
        conv = export_data.get("conversation", {})
        artifacts = export_data.get("artifacts", {})
        plans = export_data.get("plans", {})

        lines = [
            "✅ **Exportação Concluída**",
            "",
            "📊 **Estatísticas**:",
            f"  • Mensagens: {conv.get('total_messages', 0)}",
            f"  • Artifacts: {artifacts.get('count', 0)}",
            f"  • Planos: {plans.get('count', 0)}",
            f"  • Versão DEILE: {metadata.get('deile_version', __version__)}",
            f"  • ID da Sessão: {metadata.get('session_id', 'indisponível')}",
            "",
            f"📁 **Arquivos exportados ({len(exported_files)}):**",
        ]

        for fp in exported_files:
            fname = Path(fp).name
            try:
                size = Path(fp).stat().st_size
                lines.append(f"  • {fname} ({size:,} bytes)")
            except (
                Exception
            ) as exc:  # stat é best-effort no sumário — exibe sem tamanho
                logger.debug("export: falha ao ler tamanho de %s: %s", fname, exc)
                lines.append(f"  • {fname}")

        lines += [
            "",
            "🎯 **Detalhes**:",
            f"  • Formato: {format_type.upper()}",
            f"  • Gerado em: {metadata.get('generated_at', '')[:19]}",
            f"  • Fontes: {', '.join(metadata.get('data_sources', []))}",
        ]

        return Panel(
            Text("\n".join(lines), style="green"),
            title="📤 Export Concluído",
            border_style="green",
            padding=(1, 2),
        )

    def get_help(self) -> str:
        return """Exporta histórico de conversa e dados da sessão

Uso:
  /export [formato] [opções]

Formatos:
  txt      Texto simples
  md       Markdown (padrão)
  json     JSON completo
  zip      Arquivo zip com todos os dados

Opções:
  --path CAMINHO, -p CAMINHO   Diretório de destino
  --no-artifacts               Exclui artifacts
  --no-plans                   Exclui planos
  --no-session                 Exclui info da sessão
  --format FORMATO, -f         Especifica o formato

Exemplos:
  /export                           Exporta Markdown para caminho padrão
  /export zip                       Zip completo
  /export json --path ./backups     JSON em diretório customizado
  /export md --no-artifacts         Sem artifacts

Caminho padrão: ./EXPORTS/deile_export_TIMESTAMP/"""
