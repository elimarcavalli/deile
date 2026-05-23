"""
Comando /cost — rastreamento de custos, orçamentos e análise financeira
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Tuple

from deile.__version__ import __version__
from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.commands.builtin import _cost_views
from deile.commands.builtin._shared import (export_timestamp, get_session_id,
                                            split_args, success_panel)

logger = logging.getLogger(__name__)

_FORECAST_MIN_DAYS = 7


def _safe_summary_values(summary) -> Tuple[int, float]:
    """Extract (entry_count, total_amount) safely from a CostSummary."""
    count = getattr(summary, "entry_count", 0) or 0
    total = float(summary.total_amount) if summary.total_amount else 0.0
    return count, total


def _period_range(days: int) -> Tuple[datetime, datetime]:
    """Return ``(start, end)`` for the trailing ``days``-window ending now.

    Centralises the ``end = datetime.now(); start = end - timedelta(days=N)``
    pattern repeated by every cost view (summary, categories, forecast,
    export, top) — keeps a single clock point and a single semantics for
    the trailing-window so a future migration to an injectable clock has
    one site to update.
    """
    end_time = datetime.now()
    return end_time - timedelta(days=days), end_time


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
            start_time, end_time = _period_range(days)

            summary = self.cost_tracker.get_cost_summary(start_time, end_time)
            session_cost = self.cost_tracker.get_current_session_cost()

            entry_count, total_amount = _safe_summary_values(summary)
            session_cost_f = float(session_cost) if session_cost else 0.0

            content = _cost_views.build_summary_tables(
                days=days,
                total_amount=total_amount,
                entry_count=entry_count,
                session_cost=session_cost_f,
                categories=summary.categories,
            )

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

            content = _cost_views.build_session_panel(session_cost_f)
            return CommandResult.success_result(content, "rich", session_cost=session_cost_f)

        except Exception as exc:
            logger.error("Falha ao exibir custos da sessão: %s", exc)
            return CommandResult.error_result(f"Falha ao exibir sessão: {exc}", error=exc)

    def _show_categories(self) -> "CommandResult":
        try:
            start_time, end_time = _period_range(30)
            summary = self.cost_tracker.get_cost_summary(start_time, end_time)

            if not summary.categories:
                return CommandResult.success_result(
                    _cost_views.build_no_data_panel(
                        "Nenhuma categoria encontrada nos últimos 30 dias.",
                        title="📊 Custos por Categoria",
                    ),
                    "rich",
                )

            _, total_amount = _safe_summary_values(summary)
            table = _cost_views.build_categories_table(summary.categories, total_amount)
            return CommandResult.success_result(table, "rich", total=total_amount)

        except Exception as exc:
            logger.error("Falha ao exibir categorias: %s", exc)
            return CommandResult.error_result(f"Falha ao exibir categorias: {exc}", error=exc)

    def _show_budget_list(self) -> "CommandResult":
        try:
            budgets = self.cost_tracker.list_budget_limits()

            if not budgets:
                return CommandResult.success_result(
                    _cost_views.build_no_data_panel(
                        "Nenhum limite de orçamento configurado.",
                        title="📋 Orçamentos",
                    ),
                    "rich",
                )

            table = _cost_views.build_budget_list_table(budgets)
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
            hist_start, hist_end = _period_range(30)
            summary = self.cost_tracker.get_cost_summary(hist_start, hist_end)

            entry_count, total_amount = _safe_summary_values(summary)

            if entry_count == 0 or total_amount == 0.0:
                return CommandResult.success_result(
                    _cost_views.build_no_data_panel(
                        f"Dados insuficientes para previsão — mínimo {_FORECAST_MIN_DAYS} dias necessários.\n"
                        "Nenhuma entrada de custo encontrada nos últimos 30 dias.",
                        title="📈 Previsão de Custos",
                    ),
                    "rich",
                )

            daily_avg = total_amount / 30
            projected = daily_avg * forecast_days

            table = _cost_views.build_forecast_table(
                forecast_days=forecast_days,
                daily_avg=daily_avg,
                projected=projected,
                entry_count=entry_count,
            )

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
            start_time, end_time = _period_range(days)
            data = self.cost_tracker.export_costs(start_time, end_time, format_type=fmt)

            if not data:
                return CommandResult.success_result(
                    _cost_views.build_no_data_panel(
                        "Nenhum dado de custo encontrado no período.",
                        title="📤 Export de Custos",
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
            start_time, end_time = _period_range(30)
            summary = self.cost_tracker.get_cost_summary(start_time, end_time)

            top = summary.top_expenses[:n] if summary.top_expenses else []

            if not top:
                return CommandResult.success_result(
                    _cost_views.build_no_data_panel(
                        "Nenhuma despesa encontrada nos últimos 30 dias.",
                        title=f"🏆 Top {n} Despesas",
                    ),
                    "rich",
                )

            table = _cost_views.build_top_table(top, n)
            return CommandResult.success_result(table, "rich", top_count=len(top))

        except Exception as exc:
            logger.error("Falha ao listar top despesas: %s", exc)
            return CommandResult.error_result(f"Falha ao listar top despesas: {exc}", error=exc)

    def _show_alerts(self) -> "CommandResult":
        try:
            alerts = getattr(self.cost_tracker, "cost_alerts", []) or []

            if not alerts:
                return CommandResult.success_result(
                    _cost_views.build_no_alerts_panel(),
                    "rich",
                )

            table = _cost_views.build_alerts_table(alerts)
            return CommandResult.success_result(table, "rich", alert_count=len(alerts))

        except Exception as exc:
            logger.error("Falha ao listar alertas: %s", exc)
            return CommandResult.error_result(f"Falha ao listar alertas: {exc}", error=exc)

    def _show_cost_estimate(self, provider: str, model: str, tokens: int) -> "CommandResult":
        try:
            estimate = self.cost_tracker.get_pricing_estimate(provider, model, tokens)

            if "error" in estimate:
                return CommandResult.error_result(estimate["error"])

            content = _cost_views.build_estimate_panel(
                provider=provider, model=model, estimate=estimate
            )
            return CommandResult.success_result(content, "rich", estimate=estimate)

        except Exception as exc:
            logger.error("Falha ao calcular estimativa: %s", exc)
            return CommandResult.error_result(f"Falha ao calcular estimativa: {exc}", error=exc)
