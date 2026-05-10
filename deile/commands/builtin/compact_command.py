"""Comando /compact — gerenciamento de memória e histórico de sessões."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.commands.builtin._shared import (export_timestamp,
                                              get_memory_manager, split_args)

logger = logging.getLogger(__name__)


async def _get_session_store(context: CommandContext) -> Optional[Any]:
    agent = context.agent
    if agent is None:
        return None
    if hasattr(agent, "_get_session_store"):
        try:
            return await agent._get_session_store()
        except Exception as exc:
            logger.warning("Falha ao obter SessionStore do agente: %s", exc)
    return getattr(agent, "_session_store", None)


class CompactCommand(DirectCommand):
    """Gerencia memória e histórico de sessões: compactar, expurgar e analisar."""

    def __init__(self) -> None:
        super().__init__()
        self.config.description = "Gerenciar memória e histórico de sessões"
        self.compact_config: dict[str, Any] = {
            "auto_compress": True,
            "compress_threshold_days": 7,
            "purge_threshold_days": 30,
            "max_memory_usage_mb": 100,
        }

    async def execute(self, context: CommandContext) -> CommandResult:
        parts: list[str] = split_args(context)
        action = parts[0].lower() if parts else "summary"

        try:
            if action == "summary":
                return await self._cmd_summary(context)
            elif action == "compress":
                days = int(parts[1]) if len(parts) > 1 else self.compact_config["compress_threshold_days"]
                return await self._cmd_compress(context, days)
            elif action == "purge":
                days = int(parts[1]) if len(parts) > 1 else self.compact_config["purge_threshold_days"]
                confirm = "--confirm" in parts or (
                    len(parts) > 2 and parts[2].lower() in ("s", "sim", "yes", "y")
                )
                return await self._cmd_purge(context, days, confirm)
            elif action == "analyze":
                return await self._cmd_analyze(context)
            elif action == "export":
                fmt = parts[1] if len(parts) > 1 else "json"
                fname = parts[2] if len(parts) > 2 else None
                return await self._cmd_export(context, fmt, fname)
            elif action == "import":
                fname = parts[1] if len(parts) > 1 else None
                return await self._cmd_import(context, fname)
            elif action == "config":
                if len(parts) < 3:
                    return await self._cmd_show_config()
                return await self._cmd_set_config(parts[1], parts[2])
            else:
                return CommandResult.error_result(f"Ação desconhecida: {action}")
        except ValueError as exc:
            return CommandResult.error_result(f"Parâmetro inválido: {exc}")
        except Exception as exc:
            logger.error("CompactCommand falhou: %s", exc)
            return CommandResult.error_result(f"Falha na execução: {exc}")

    # ------------------------------------------------------------------
    # /compact summary
    # ------------------------------------------------------------------

    async def _cmd_summary(self, context: CommandContext) -> CommandResult:
        mm = get_memory_manager(context)
        ss = await _get_session_store(context)

        table = Table(title="Resumo de Memória e Sessões", show_header=True, header_style="bold cyan")
        table.add_column("Métrica", style="white")
        table.add_column("Valor", style="green")
        table.add_column("Detalhes", style="dim")

        if mm is not None:
            try:
                usage = await mm.get_memory_usage()
                total_mb = usage.get("total_memory_mb", usage.get("total_memory", 0))
                components = usage.get("components", {})
                wm = components.get("working_memory", {})
                table.add_row(
                    "Uso de memória",
                    f"{total_mb:.1f} MB",
                    f"Limite: {self.compact_config['max_memory_usage_mb']} MB",
                )
                table.add_row(
                    "Working memory (entradas)",
                    str(wm.get("total_entries", "—")),
                    f"TTL: {wm.get('ttl', '—')}s",
                )
            except Exception as exc:
                table.add_row("Memória", "INDISPONÍVEL", str(exc)[:50])
        else:
            table.add_row("Memória", "INDISPONÍVEL", "MemoryManager não acessível")

        if ss is not None:
            try:
                stats = await ss.get_stats()
                count = stats.get("session_count", 0)
                oldest = stats.get("oldest_last_used") or "—"
                newest = stats.get("newest_last_used") or "—"
                table.add_row("Sessões armazenadas", str(count), f"Mais recente: {newest[:10]}")
                table.add_row("Sessão mais antiga", oldest[:10] if oldest != "—" else "—", "")
            except Exception as exc:
                table.add_row("Sessões", "INDISPONÍVEL", str(exc)[:50])
        else:
            table.add_row("Sessões", "INDISPONÍVEL", "SessionStore não acessível")

        return CommandResult.success_result(content=table, content_type="rich")

    # ------------------------------------------------------------------
    # /compact compress <days>
    # ------------------------------------------------------------------

    async def _cmd_compress(self, context: CommandContext, days: int) -> CommandResult:
        mm = get_memory_manager(context)
        if mm is None:
            return CommandResult.error_result(
                "MemoryManager não acessível — compactação indisponível"
            )

        try:
            report = await mm.consolidate(older_than_days=days)
        except Exception as exc:
            return CommandResult.error_result(f"Falha na consolidação: {exc}")

        entries_before = report.get("entries_before", 0)
        entries_processed = report.get("entries_processed", 0)
        total_time = report.get("total_time_s", 0.0)

        text = Text()
        text.append("Compactação concluída\n\n", style="bold green")
        text.append(f"Limiar: {days} dias\n")
        text.append(f"Entradas antes: {entries_before}\n")
        text.append(f"Entradas processadas: {entries_processed}\n")
        text.append(f"Tempo total: {total_time:.3f}s")

        panel = Panel(text, title="Resultado da Compactação", border_style="green")
        return CommandResult.success_result(
            content=panel,
            content_type="rich",
            entries_before=entries_before,
            entries_processed=entries_processed,
        )

    # ------------------------------------------------------------------
    # /compact purge <days> [--confirm | sim | yes | s | y]
    # ------------------------------------------------------------------

    async def _cmd_purge(
        self, context: CommandContext, days: int, confirm: bool
    ) -> CommandResult:
        if days < 1:
            return CommandResult.error_result(
                "Limiar mínimo de expurgo é 1 dia. Use /compact purge <dias> com dias ≥ 1."
            )

        ss = await _get_session_store(context)
        if ss is None:
            return CommandResult.error_result(
                "SessionStore não acessível — expurgo indisponível"
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            count = await ss.count_sessions_before(cutoff)
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao contar sessões: {exc}")

        if not confirm:
            text = Text()
            text.append(
                f"Isso deletará permanentemente {count} sessão(ões) anteriores a "
                f"{cutoff.strftime('%Y-%m-%d')}. Confirmar? [s/N]\n\n",
                style="bold yellow",
            )
            text.append(f"Para confirmar: /compact purge {days} --confirm")
            panel = Panel(text, title="Confirmação de Expurgo", border_style="yellow")
            return CommandResult.success_result(
                content=panel,
                content_type="rich",
                sessions_to_delete=count,
                purged_count=0,
                confirmed=False,
            )

        try:
            deleted = await ss.delete_sessions_before(cutoff)
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao expurgar sessões: {exc}")

        text = Text()
        text.append("Expurgo concluído\n\n", style="bold green")
        text.append(f"Sessões deletadas: {deleted}\n")
        text.append(f"Limiar: {days} dias (anterior a {cutoff.strftime('%Y-%m-%d')})")
        panel = Panel(text, title="Resultado do Expurgo", border_style="green")
        return CommandResult.success_result(
            content=panel,
            content_type="rich",
            purged_count=deleted,
            confirmed=True,
        )

    # ------------------------------------------------------------------
    # /compact analyze
    # ------------------------------------------------------------------

    async def _cmd_analyze(self, context: CommandContext) -> CommandResult:
        ss = await _get_session_store(context)
        if ss is None:
            return CommandResult.error_result(
                "SessionStore não acessível — análise indisponível"
            )

        try:
            sessions = await ss.list_all()
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao listar sessões: {exc}")

        if not sessions:
            text = Text()
            text.append(
                "Análise de tópicos: dados insuficientes (0 sessões encontradas)",
                style="yellow",
            )
            return CommandResult.success_result(
                content=Panel(text, title="Análise", border_style="yellow"),
                content_type="rich",
                session_count=0,
            )

        dates: list[datetime] = []
        for s in sessions:
            raw = s.get("last_used_at", "")
            if raw:
                try:
                    dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
                except ValueError:
                    pass

        table = Table(title="Análise de Sessões", show_header=True, header_style="bold cyan")
        table.add_column("Métrica", style="white")
        table.add_column("Valor", style="green")
        table.add_column("Detalhes", style="dim")

        table.add_row("Total de sessões", str(len(sessions)), "")

        if dates:
            oldest = min(dates)
            newest = max(dates)
            span_days = (newest - oldest).days
            table.add_row("Sessão mais antiga", oldest.strftime("%Y-%m-%d"), "")
            table.add_row("Sessão mais recente", newest.strftime("%Y-%m-%d"), "")
            table.add_row("Intervalo total", f"{span_days} dia(s)", "")

            hour_counts: Counter = Counter(d.hour for d in dates)
            if hour_counts:
                peak_hour, peak_n = hour_counts.most_common(1)[0]
                peak_pct = 100 * peak_n / len(dates)
                table.add_row(
                    "Hora mais ativa",
                    f"{peak_hour:02d}:00",
                    f"{peak_pct:.1f}% das sessões",
                )

        if len(sessions) < 5:
            tópicos_msg = f"dados insuficientes ({len(sessions)} sessão(ões) encontradas)"
        else:
            tópicos_msg = f"baseada em {len(sessions)} sessões de metadados reais"
        table.add_row("Análise de tópicos", tópicos_msg, "")

        return CommandResult.success_result(
            content=table,
            content_type="rich",
            session_count=len(sessions),
        )

    # ------------------------------------------------------------------
    # /compact export <format> [filename]
    # ------------------------------------------------------------------

    async def _cmd_export(
        self, context: CommandContext, fmt: str, filename: Optional[str]
    ) -> CommandResult:
        if fmt not in ("json", "text", "csv"):
            return CommandResult.error_result(
                f"Formato inválido: {fmt}. Use json, text ou csv."
            )

        ss = await _get_session_store(context)
        if ss is None:
            return CommandResult.error_result(
                "SessionStore não acessível — exportação indisponível"
            )

        try:
            sessions = await ss.list_all()
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao listar sessões: {exc}")

        if not sessions:
            return CommandResult.error_result("Nenhuma sessão encontrada para exportar")

        if not filename:
            filename = f"deile_sessoes_{export_timestamp()}.{fmt}"

        export_path = Path(filename)

        try:
            if fmt == "json":
                content = json.dumps(sessions, indent=2, ensure_ascii=False)
                await asyncio.to_thread(export_path.write_text, content, "utf-8")
            elif fmt == "text":
                lines = []
                for s in sessions:
                    lines.append(f"Sessão: {s['session_id']}")
                    lines.append(f"Último uso: {s['last_used_at']}")
                    lines.append("-" * 50)
                await asyncio.to_thread(export_path.write_text, "\n".join(lines), "utf-8")
            elif fmt == "csv":
                fields = list(sessions[0].keys()) if sessions else ["session_id", "last_used_at"]

                def _write_csv() -> None:
                    with open(export_path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(sessions)

                await asyncio.to_thread(_write_csv)
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao escrever arquivo: {exc}")

        file_size = export_path.stat().st_size
        text = Text()
        text.append("Exportação concluída\n\n", style="bold green")
        text.append(f"Arquivo: {export_path}\n")
        text.append(f"Formato: {fmt.upper()}\n")
        text.append(f"Sessões: {len(sessions)}\n")
        text.append(f"Tamanho: {file_size / 1024:.1f} KB")
        panel = Panel(text, title="Resultado da Exportação", border_style="green")
        return CommandResult.success_result(
            content=panel,
            content_type="rich",
            export_file=str(export_path),
            session_count=len(sessions),
        )

    # ------------------------------------------------------------------
    # /compact import <filename>
    # ------------------------------------------------------------------

    async def _cmd_import(
        self, context: CommandContext, filename: Optional[str]
    ) -> CommandResult:
        if not filename:
            return CommandResult.error_result(
                "Nome do arquivo é obrigatório para importação"
            )

        import_path = Path(filename)
        if not import_path.exists():
            return CommandResult.error_result(f"Arquivo não encontrado: {filename}")

        ss = await _get_session_store(context)
        if ss is None:
            return CommandResult.error_result(
                "SessionStore não acessível — importação indisponível"
            )

        suffix = import_path.suffix.lower().lstrip(".")
        if suffix not in ("json", "csv"):
            return CommandResult.error_result(
                f"Formato não suportado: {suffix}. Use json ou csv."
            )

        try:
            if suffix == "json":
                raw = await asyncio.to_thread(import_path.read_text, "utf-8")
                sessions = json.loads(raw)
            else:
                def _read_csv() -> list:
                    with open(import_path, encoding="utf-8") as f:
                        return list(csv.DictReader(f))

                sessions = await asyncio.to_thread(_read_csv)
        except Exception as exc:
            return CommandResult.error_result(f"Falha ao ler arquivo: {exc}")

        if not sessions:
            return CommandResult.error_result("Nenhuma sessão válida encontrada no arquivo")

        imported = 0
        for s in sessions:
            sid = s.get("session_id")
            if not sid:
                continue
            try:
                await ss.upsert(sid, s.get("working_directory", "."), {})
                imported += 1
            except Exception:
                continue

        text = Text()
        text.append("Importação concluída\n\n", style="bold green")
        text.append(f"Arquivo: {import_path}\n")
        text.append(f"Sessões importadas: {imported}")
        panel = Panel(text, title="Resultado da Importação", border_style="green")
        return CommandResult.success_result(
            content=panel,
            content_type="rich",
            sessions_imported=imported,
        )

    # ------------------------------------------------------------------
    # /compact config [key value]
    # ------------------------------------------------------------------

    async def _cmd_show_config(self) -> CommandResult:
        table = Table(
            title="Configuração do Compact (sessão atual)",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Parâmetro", style="white")
        table.add_column("Valor", style="green")
        table.add_column("Descrição", style="dim")
        descriptions = {
            "auto_compress": "Compactação automática habilitada",
            "compress_threshold_days": "Dias antes de compactar entradas",
            "purge_threshold_days": "Dias antes de expurgar sessões",
            "max_memory_usage_mb": "Limite de uso de memória (MB)",
        }
        for key, val in self.compact_config.items():
            table.add_row(key, str(val), descriptions.get(key, ""))
        return CommandResult.success_result(content=table, content_type="rich")

    async def _cmd_set_config(self, key: str, value: str) -> CommandResult:
        if key not in self.compact_config:
            return CommandResult.error_result(f"Parâmetro desconhecido: {key}")
        original = self.compact_config[key]
        try:
            if isinstance(original, bool):
                new_val: Any = value.lower() in ("true", "on", "sim", "yes", "1")
            elif isinstance(original, int):
                new_val = int(value)
            elif isinstance(original, float):
                new_val = float(value)
            else:
                new_val = value
        except ValueError as exc:
            return CommandResult.error_result(f"Valor inválido para {key}: {exc}")
        self.compact_config[key] = new_val
        text = Text()
        text.append("Configuração atualizada (sessão atual)\n\n", style="bold green")
        text.append(f"{key}: {original} → {new_val}")
        panel = Panel(text, title="Config atualizado", border_style="green")
        return CommandResult.success_result(
            content=panel,
            content_type="rich",
            setting=key,
            new_value=new_val,
        )
