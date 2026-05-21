"""
Comando /cost — rastreamento de custos, orçamentos e análise financeira
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Tuple

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deile.__version__ import __version__
from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.commands.builtin._shared import (export_timestamp, get_session_id,
                                            split_args, success_panel)

logger = logging.getLogger(__name__)

_FORECAST_MIN_DAYS = 7


def _safe_summary_values(summary) -> Tuple[int, float]:
    """Extract (entry_count, total_amount) safely from a CostSummary."""
    count = getattr(summary, "entry_count", 0) or 0
    total = float(summary.total_amount) if summary.total_amount else 0.0
    return count, total


class CostCommand(DirectCommand):
    """Rastreamento de custos, orçamentos e análise financeira"""

    cli_flag = "--cost"
    cli_help = "Exibe custos acumulados da sessão e encerra."
    cli_requires_provider = False

    def __init__(self):
        super().__init__()
        self.config.description = "Rastreamento de custos, orçamentos e análise financeira"
        self.help_text = """
Comando /cost — Gestão Financeira e Análise

USO:
    /cost [ação] [opções]

AÇÕES:
    summary [dias]                   Resumo de custos (padrão: 30 dias)
    session                          Custos da sessão atual
    categories                       Custos por categoria real do banco
    budget list                      Lista limites de orçamento
    budget set <cat> <período> <val> Define limite de orçamento
    forecast [dias]                  Previsão de custos (padrão: 7 dias)
    export [formato] [dias]          Exporta dados (json, csv)
    estimate <prov> <model> <tokens> Estima custo de chamada API
    top [n]                          Exibe top-N despesas
    alerts                           Exibe alertas de orçamento

PERÍODOS:
    daily, weekly, monthly, yearly

CATEGORIAS (reais do banco):
    api_calls, compute, storage, network, model_usage,
    sandbox, infrastructure, external_services

EXEMPLOS:
    /cost summary                           # Últimos 30 dias
    /cost summary 7                         # Últimos 7 dias
    /cost session                           # Custos da sessão atual
    /cost budget set api_calls monthly 100  # $100/mês para API
    /cost forecast 14                       # Previsão 14 dias
    /cost export json 90                    # Exporta últimos 90 dias
    /cost estimate gemini pro 5000          # Estima 5000 tokens
"""
        self._cost_tracker: Any = None

    @property
    def cost_tracker(self):
        """Cost tracker resolvido sob demanda — evita import eager de
        ``deile.infrastructure`` na camada de comandos (Clean Arch §2)."""
        if self._cost_tracker is None:
            from deile.infrastructure.monitoring.cost_tracker import \
                get_cost_tracker
            self._cost_tracker = get_cost_tracker()
        return self._cost_tracker

    async def execute(self, context: CommandContext) -> CommandResult:
        args_list: List[str] = split_args(context)

        session_id = get_session_id(context)

        try:
            if not args_list:
                return self._show_cost_summary()

            action = args_list[0].lower()

            if action == "summary":
                days = int(args_list[1]) if len(args_list) > 1 else 30
                return self._show_cost_summary(days)
            if action == "session":
                return self._show_session_costs(session_id)
            if action == "categories":
                return self._show_categories()
            if action == "budget":
                sub = args_list[1].lower() if len(args_list) > 1 else "list"
                if sub == "list":
                    return self._show_budget_list()
                if sub == "set":
                    if len(args_list) < 5:
                        return CommandResult.error_result(
                            "Uso: /cost budget set <categoria> <período> <valor>"
                        )
                    return self._set_budget(args_list[2], args_list[3], args_list[4])
                return CommandResult.error_result(f"Subcomando de budget desconhecido: {sub}")
            if action == "forecast":
                days = int(args_list[1]) if len(args_list) > 1 else 7
                return self._show_forecast(days)
            if action == "export":
                fmt = args_list[1] if len(args_list) > 1 else "json"
                days = int(args_list[2]) if len(args_list) > 2 else 30
                return await self._export_costs(fmt, days)
            if action == "estimate":
                if len(args_list) < 4:
                    return CommandResult.error_result(
                        "Uso: /cost estimate <provedor> <modelo> <tokens>"
                    )
                provider, model, tokens = args_list[1], args_list[2], int(args_list[3])
                return self._show_cost_estimate(provider, model, tokens)
            if action == "top":
                n = int(args_list[1]) if len(args_list) > 1 else 5
                return self._show_top(n)
            if action == "alerts":
                return self._show_alerts()

            return CommandResult.error_result(f"Ação desconhecida: {action}")

        except ValueError as exc:
            return CommandResult.error_result(f"Parâmetro inválido: {exc}")
        except Exception as exc:
            logger.error("Erro na execução do CostCommand: %s", exc)
            return CommandResult.error_result(f"Falha na execução: {exc}", error=exc)

    def _show_cost_summary(self, days: int = 30) -> "CommandResult":
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)

            summary = self.cost_tracker.get_cost_summary(start_time, end_time)
            session_cost = self.cost_tracker.get_current_session_cost()

            entry_count, total_amount = _safe_summary_values(summary)
            session_cost_f = float(session_cost) if session_cost else 0.0

            summary_table = Table(
                title=f"💰 Resumo de Custos ({days} dias)",
                show_header=True,
                header_style="bold cyan",
            )
            summary_table.add_column("Métrica", style="white", width=22)
            summary_table.add_column("Valor", style="green", width=20)
            summary_table.add_column("Detalhes", style="dim", width=30)

            summary_table.add_row(
                "Total Gasto",
                f"${total_amount:.4f}",
                f"{entry_count} transações",
            )
            daily_avg = total_amount / days if days > 0 else 0.0
            summary_table.add_row(
                "Média Diária",
                f"${daily_avg:.4f}",
                f"Baseado em {days} dias",
            )
            summary_table.add_row(
                "Sessão Atual",
                f"${session_cost_f:.6f}",
                "Custo da sessão ativa",
            )
            if total_amount > 0 and entry_count > 0:
                avg_per = total_amount / entry_count
                summary_table.add_row(
                    "Média por Transação",
                    f"${avg_per:.6f}",
                    "Por entrada de custo",
                )

            if summary.categories:
                category_table = Table(
                    title="📊 Custos por Categoria",
                    show_header=True,
                    header_style="bold yellow",
                )
                category_table.add_column("Categoria", style="cyan", width=22)
                category_table.add_column("Valor", style="green", width=14)
                category_table.add_column("Percentual", style="white", width=14)
                category_table.add_column("Visual", style="blue", width=20)

                for category, amount in sorted(
                    summary.categories.items(), key=lambda x: x[1], reverse=True
                ):
                    pct = float(amount) / total_amount * 100 if total_amount > 0 else 0
                    bar = "█" * min(int(pct / 5), 20)
                    category_table.add_row(category, f"${float(amount):.4f}", f"{pct:.1f}%", bar)

                content = Group(summary_table, "", category_table)
            else:
                no_data = Panel(
                    Text("Nenhum dado de custo no período selecionado.", style="yellow"),
                    title="📊 Categorias",
                    border_style="yellow",
                )
                content = Group(summary_table, "", no_data)

            return CommandResult.success_result(
                content,
                "rich",
                total_amount=total_amount,
                session_cost=session_cost_f,
                period_days=days,
                entry_count=entry_count,
                categories={k: float(v) for k, v in summary.categories.items()},
            )

        except Exception as exc:
            logger.error("Falha ao exibir resumo de custos: %s", exc)
            return CommandResult.error_result(f"Falha ao exibir resumo: {exc}", error=exc)

    def _show_session_costs(self, session_id=None) -> "CommandResult":
        try:
            session_cost = self.cost_tracker.get_current_session_cost(session_id)
            session_cost_f = float(session_cost) if session_cost else 0.0

            info = (
                f"💰 **Custo da Sessão Atual**: ${session_cost_f:.6f}\n\n"
                "Representa o custo de chamadas de API e uso de recursos\n"
                "na sessão DEILE atual.\n\n"
                "📊 **Incluído**:\n"
                "• Chamadas a modelos de linguagem\n"
                "• Uso de recursos de computação\n"
                "• Uso de rede e armazenamento\n\n"
                "💡 **Custo resetado ao iniciar nova sessão**"
            )
            style = "green" if session_cost_f > 0 else "blue"
            suffix = "\n\n📈 **Sessão ativa com custos**" if session_cost_f > 0 else "\n\n🎉 **Sem custos nesta sessão!**"

            content = Panel(
                Text(info + suffix, style=style),
                title="💰 Custos da Sessão",
                border_style=style,
            )
            return CommandResult.success_result(content, "rich", session_cost=session_cost_f)

        except Exception as exc:
            logger.error("Falha ao exibir custos da sessão: %s", exc)
            return CommandResult.error_result(f"Falha ao exibir sessão: {exc}", error=exc)

    def _show_categories(self) -> "CommandResult":
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=30)
            summary = self.cost_tracker.get_cost_summary(start_time, end_time)

            if not summary.categories:
                return CommandResult.success_result(
                    Panel(
                        Text("Nenhuma categoria encontrada nos últimos 30 dias.", style="yellow"),
                        title="📊 Custos por Categoria",
                        border_style="yellow",
                    ),
                    "rich",
                )

            table = Table(
                title="📊 Custos por Categoria (30 dias)",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Categoria", style="cyan", width=24)
            table.add_column("Valor", style="green", width=14)
            table.add_column("Percentual", style="white", width=12)

            _, total_amount = _safe_summary_values(summary)

            for category in sorted(summary.categories, key=lambda c: summary.categories[c], reverse=True):
                cat_amount = float(summary.categories[category])
                pct = cat_amount / total_amount * 100 if total_amount > 0 else 0.0
                table.add_row(category, f"${cat_amount:.4f}", f"{pct:.1f}%")

            return CommandResult.success_result(table, "rich", total=total_amount)

        except Exception as exc:
            logger.error("Falha ao exibir categorias: %s", exc)
            return CommandResult.error_result(f"Falha ao exibir categorias: {exc}", error=exc)

    def _show_budget_list(self) -> "CommandResult":
        try:
            budgets = self.cost_tracker.list_budget_limits()

            if not budgets:
                return CommandResult.success_result(
                    Panel(
                        Text("Nenhum limite de orçamento configurado.", style="yellow"),
                        title="📋 Orçamentos",
                        border_style="yellow",
                    ),
                    "rich",
                )

            table = Table(
                title="📋 Limites de Orçamento",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Categoria", style="cyan", width=22)
            table.add_column("Período", style="white", width=12)
            table.add_column("Limite", style="green", width=12)
            table.add_column("Alerta em", style="yellow", width=10)
            table.add_column("Rígido", style="red", width=8)

            for budget in budgets.values():
                table.add_row(
                    budget.category,
                    budget.period,
                    f"${float(budget.limit_amount):.2f}",
                    f"{budget.alert_threshold * 100:.0f}%",
                    "Sim" if budget.hard_limit else "Não",
                )

            return CommandResult.success_result(table, "rich")

        except Exception as exc:
            logger.error("Falha ao listar orçamentos: %s", exc)
            return CommandResult.error_result(f"Falha ao listar orçamentos: {exc}", error=exc)

    def _set_budget(self, category: str, period: str, amount_str: str) -> "CommandResult":
        try:
            amount = float(amount_str)
            if amount < 0:
                return CommandResult.error_result("O valor do limite deve ser positivo.")

            ok = self.cost_tracker.set_budget_limit(category, period, amount)
            if ok:
                msg = f"✅ Limite definido: {category}/{period} = ${amount:.2f}"
                return CommandResult.success_result(
                    success_panel(msg, title="💾 Orçamento Salvo"),
                    "rich",
                    category=category,
                    period=period,
                    amount=amount,
                )
            return CommandResult.error_result("Falha ao salvar limite de orçamento.")

        except ValueError:
            return CommandResult.error_result(f"Valor inválido: '{amount_str}'. Use número decimal.")
        except Exception as exc:
            logger.error("Falha ao definir orçamento: %s", exc)
            return CommandResult.error_result(f"Falha ao definir orçamento: {exc}", error=exc)

    def _show_forecast(self, forecast_days: int = 7) -> "CommandResult":
        try:
            hist_end = datetime.now()
            hist_start = hist_end - timedelta(days=30)
            summary = self.cost_tracker.get_cost_summary(hist_start, hist_end)

            entry_count, total_amount = _safe_summary_values(summary)

            if entry_count == 0 or total_amount == 0.0:
                return CommandResult.success_result(
                    Panel(
                        Text(
                            f"Dados insuficientes para previsão — mínimo {_FORECAST_MIN_DAYS} dias necessários.\n"
                            "Nenhuma entrada de custo encontrada nos últimos 30 dias.",
                            style="yellow",
                        ),
                        title="📈 Previsão de Custos",
                        border_style="yellow",
                    ),
                    "rich",
                )

            daily_avg = total_amount / 30
            projected = daily_avg * forecast_days

            table = Table(
                title=f"📈 Previsão de Custos ({forecast_days} dias)",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Métrica", style="white", width=28)
            table.add_column("Valor", style="green", width=20)

            table.add_row("Média diária (30 dias)", f"${daily_avg:.4f}")
            table.add_row(f"Previsão ({forecast_days} dias)", f"${projected:.4f}")
            table.add_row("Método", "Projeção linear (média × dias)")
            table.add_row("Observações históricas", str(entry_count))

            return CommandResult.success_result(
                table,
                "rich",
                daily_avg=daily_avg,
                projected=projected,
                forecast_days=forecast_days,
            )

        except Exception as exc:
            logger.error("Falha ao calcular previsão: %s", exc)
            return CommandResult.error_result(f"Falha ao calcular previsão: {exc}", error=exc)

    async def _export_costs(self, fmt: str = "json", days: int = 30) -> "CommandResult":
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)
            data = self.cost_tracker.export_costs(start_time, end_time, format_type=fmt)

            if not data:
                return CommandResult.success_result(
                    Panel(
                        Text("Nenhum dado de custo encontrado no período.", style="yellow"),
                        title="📤 Export de Custos",
                        border_style="yellow",
                    ),
                    "rich",
                )

            ext = "csv" if fmt == "csv" else "json"
            fname = f"costs_export_{export_timestamp()}.{ext}"
            await asyncio.to_thread(Path(fname).write_text, data, encoding="utf-8")

            msg = (
                f"✅ Exportado: {fname}\n"
                f"Período: {start_time.strftime('%Y-%m-%d')} → {end_time.strftime('%Y-%m-%d')}\n"
                f"Versão DEILE: {__version__}"
            )
            return CommandResult.success_result(
                success_panel(msg, title="📤 Export de Custos"),
                "rich",
                file=fname,
            )

        except Exception as exc:
            logger.error("Falha ao exportar custos: %s", exc)
            return CommandResult.error_result(f"Falha ao exportar: {exc}", error=exc)

    def _show_top(self, n: int = 5) -> "CommandResult":
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=30)
            summary = self.cost_tracker.get_cost_summary(start_time, end_time)

            top = summary.top_expenses[:n] if summary.top_expenses else []

            if not top:
                return CommandResult.success_result(
                    Panel(
                        Text("Nenhuma despesa encontrada nos últimos 30 dias.", style="yellow"),
                        title=f"🏆 Top {n} Despesas",
                        border_style="yellow",
                    ),
                    "rich",
                )

            table = Table(
                title=f"🏆 Top {n} Despesas (30 dias)",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("#", style="dim", width=4)
            table.add_column("Categoria", style="cyan", width=20)
            table.add_column("Subcategoria", style="white", width=20)
            table.add_column("Valor", style="green", width=14)
            table.add_column("Descrição", style="dim", width=30)

            for idx, entry in enumerate(top, 1):
                table.add_row(
                    str(idx),
                    entry.get("category", "—"),
                    entry.get("subcategory", "—"),
                    f"${entry.get('amount', 0):.6f}",
                    (entry.get("description", "—") or "—")[:28],
                )

            return CommandResult.success_result(table, "rich", top_count=len(top))

        except Exception as exc:
            logger.error("Falha ao listar top despesas: %s", exc)
            return CommandResult.error_result(f"Falha ao listar top despesas: {exc}", error=exc)

    def _show_alerts(self) -> "CommandResult":
        try:
            alerts = getattr(self.cost_tracker, "cost_alerts", []) or []

            if not alerts:
                return CommandResult.success_result(
                    Panel(
                        Text("Nenhum alerta de orçamento ativo.", style="green"),
                        title="🔔 Alertas de Orçamento",
                        border_style="green",
                    ),
                    "rich",
                )

            table = Table(
                title="🔔 Alertas de Orçamento",
                show_header=True,
                header_style="bold red",
            )
            table.add_column("Tipo", style="red", width=20)
            table.add_column("Categoria", style="cyan", width=18)
            table.add_column("Uso Atual", style="yellow", width=14)
            table.add_column("% Limite", style="white", width=10)

            for alert in alerts:
                table.add_row(
                    alert.get("alert_type", "—"),
                    alert.get("category", "—"),
                    f"${float(alert.get('current_usage', 0)):.4f}",
                    f"{alert.get('percentage', 0) * 100:.1f}%",
                )

            return CommandResult.success_result(table, "rich", alert_count=len(alerts))

        except Exception as exc:
            logger.error("Falha ao listar alertas: %s", exc)
            return CommandResult.error_result(f"Falha ao listar alertas: {exc}", error=exc)

    def _show_cost_estimate(self, provider: str, model: str, tokens: int) -> "CommandResult":
        try:
            estimate = self.cost_tracker.get_pricing_estimate(provider, model, tokens)

            if "error" in estimate:
                return CommandResult.error_result(estimate["error"])

            table = Table(
                title="💰 Estimativa de Custo",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Componente", style="white", width=20)
            table.add_column("Tokens", style="yellow", width=14)
            table.add_column("Custo", style="green", width=14)

            input_t = estimate.get("estimated_input_tokens", 0)
            output_t = estimate.get("estimated_output_tokens", 0)
            total_t = estimate.get("estimated_total_tokens", 0)
            input_c = estimate.get("estimated_input_cost", 0)
            output_c = estimate.get("estimated_output_cost", 0)
            total_c = estimate.get("estimated_total_cost", 0)

            table.add_row("Tokens de entrada", f"{input_t:,}", f"${input_c:.6f}")
            table.add_row("Tokens de saída", f"{output_t:,}", f"${output_c:.6f}")
            table.add_row("**Total**", f"**{total_t:,}**", f"**${total_c:.6f}**")

            details = Panel(
                Text(
                    f"Provedor: {estimate.get('provider', provider)}\n"
                    f"Modelo: {estimate.get('model', model)}\n"
                    f"Moeda: {estimate.get('currency', 'USD')}\n\n"
                    "💡 Estimativa baseada em padrões típicos de uso.",
                    style="blue",
                ),
                title="🔍 Detalhes",
                border_style="blue",
            )

            return CommandResult.success_result(
                Group(table, "", details), "rich", estimate=estimate
            )

        except Exception as exc:
            logger.error("Falha ao calcular estimativa: %s", exc)
            return CommandResult.error_result(f"Falha ao calcular estimativa: {exc}", error=exc)
