"""Pure Rich-rendering helpers for ``/cost`` subcommands.

Extracted from :class:`CostCommand` to keep the command focused on
dispatch + side effects (cost_tracker queries, budget mutations, file
exports) while the visual layer becomes independently testable. Same
separation already applied to ``/model`` (`_model_views.py`) and
``/status`` (`_status_collectors.py`).

Each helper takes plain data — no ``self``, no cost tracker, no
registry — and returns a Rich renderable. The caller is responsible
for fetching that data from the right subsystem.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def build_no_data_panel(message: str, title: str) -> Panel:
    """Yellow-border ``Panel`` for empty-state messages.

    The ``Panel(Text(msg, style="yellow"), title=..., border_style="yellow")``
    pattern was duplicated ~6 times across ``cost_command``; centralising
    here keeps the empty-state visual consistent (and shrinks the call sites
    to a single line).
    """
    return Panel(Text(message, style="yellow"), title=title, border_style="yellow")


def build_summary_tables(
    *,
    days: int,
    total_amount: float,
    entry_count: int,
    session_cost: float,
    categories: Mapping[str, Any],
) -> Group:
    """Render the ``/cost summary`` view (overall table + categories table).

    ``categories`` is ``CostSummary.categories`` (``Dict[str, Decimal]``);
    when empty, a yellow "no data" panel replaces the categories table.
    """
    summary_table = Table(
        title=f"💰 Resumo de Custos ({days} dias)",
        show_header=True,
        header_style="bold cyan",
    )
    summary_table.add_column("Métrica", style="white")
    summary_table.add_column("Valor", style="green")
    summary_table.add_column("Detalhes", style="dim")

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
        f"${session_cost:.6f}",
        "Custo da sessão ativa",
    )
    if total_amount > 0 and entry_count > 0:
        avg_per = total_amount / entry_count
        summary_table.add_row(
            "Média por Transação",
            f"${avg_per:.6f}",
            "Por entrada de custo",
        )

    if categories:
        category_table = Table(
            title="📊 Custos por Categoria",
            show_header=True,
            header_style="bold yellow",
        )
        category_table.add_column("Categoria", style="cyan")
        category_table.add_column("Valor", style="green")
        category_table.add_column("Percentual", style="white")
        category_table.add_column("Visual", style="blue")

        for category, amount in sorted(
            categories.items(), key=lambda x: x[1], reverse=True
        ):
            pct = float(amount) / total_amount * 100 if total_amount > 0 else 0
            bar = "█" * min(int(pct / 5), 20)
            category_table.add_row(
                category, f"${float(amount):.4f}", f"{pct:.1f}%", bar
            )
        return Group(summary_table, "", category_table)

    no_data = build_no_data_panel(
        "Nenhum dado de custo no período selecionado.",
        title="📊 Categorias",
    )
    return Group(summary_table, "", no_data)


def build_session_panel(session_cost: float) -> Panel:
    """Render the ``/cost session`` panel."""
    info = (
        f"💰 **Custo da Sessão Atual**: ${session_cost:.6f}\n\n"
        "Representa o custo de chamadas de API e uso de recursos\n"
        "na sessão DEILE atual.\n\n"
        "📊 **Incluído**:\n"
        "• Chamadas a modelos de linguagem\n"
        "• Uso de recursos de computação\n"
        "• Uso de rede e armazenamento\n\n"
        "💡 **Custo resetado ao iniciar nova sessão**"
    )
    style = "green" if session_cost > 0 else "blue"
    suffix = (
        "\n\n📈 **Sessão ativa com custos**"
        if session_cost > 0
        else "\n\n🎉 **Sem custos nesta sessão!**"
    )
    return Panel(
        Text(info + suffix, style=style),
        title="💰 Custos da Sessão",
        border_style=style,
    )


def build_categories_table(categories: Mapping[str, Any], total_amount: float) -> Table:
    """Render the ``/cost categories`` table (30-day window)."""
    table = Table(
        title="📊 Custos por Categoria (30 dias)",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Categoria", style="cyan")
    table.add_column("Valor", style="green")
    table.add_column("Percentual", style="white")

    for category in sorted(categories, key=lambda c: categories[c], reverse=True):
        cat_amount = float(categories[category])
        pct = cat_amount / total_amount * 100 if total_amount > 0 else 0.0
        table.add_row(category, f"${cat_amount:.4f}", f"{pct:.1f}%")
    return table


def build_budget_list_table(budgets: Mapping[str, Any]) -> Table:
    """Render the ``/cost budget list`` table.

    ``budgets`` is a mapping whose values are ``BudgetLimit`` instances
    (``category``, ``period``, ``limit_amount``, ``alert_threshold``,
    ``hard_limit``).
    """
    table = Table(
        title="📋 Limites de Orçamento",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Categoria", style="cyan")
    table.add_column("Período", style="white")
    table.add_column("Limite", style="green")
    table.add_column("Alerta em", style="yellow")
    table.add_column("Rígido", style="red")

    for budget in budgets.values():
        table.add_row(
            budget.category,
            budget.period,
            f"${float(budget.limit_amount):.2f}",
            f"{budget.alert_threshold * 100:.0f}%",
            "Sim" if budget.hard_limit else "Não",
        )
    return table


def build_forecast_table(
    *, forecast_days: int, daily_avg: float, projected: float, entry_count: int
) -> Table:
    """Render the ``/cost forecast`` table (linear projection)."""
    table = Table(
        title=f"📈 Previsão de Custos ({forecast_days} dias)",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Métrica", style="white")
    table.add_column("Valor", style="green")

    table.add_row("Média diária (30 dias)", f"${daily_avg:.4f}")
    table.add_row(f"Previsão ({forecast_days} dias)", f"${projected:.4f}")
    table.add_row("Método", "Projeção linear (média × dias)")
    table.add_row("Observações históricas", str(entry_count))
    return table


def build_top_table(top: Sequence[Mapping[str, Any]], n: int) -> Table:
    """Render the ``/cost top`` table (top-N most expensive entries)."""
    table = Table(
        title=f"🏆 Top {n} Despesas (30 dias)",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim")
    table.add_column("Categoria", style="cyan")
    table.add_column("Subcategoria", style="white")
    table.add_column("Valor", style="green")
    table.add_column("Descrição", style="dim")

    for idx, entry in enumerate(top, 1):
        table.add_row(
            str(idx),
            entry.get("category", "—"),
            entry.get("subcategory", "—"),
            f"${entry.get('amount', 0):.6f}",
            (entry.get("description", "—") or "—")[:28],
        )
    return table


def build_alerts_table(alerts: Sequence[Mapping[str, Any]]) -> Table:
    """Render the ``/cost alerts`` table."""
    table = Table(
        title="🔔 Alertas de Orçamento",
        show_header=True,
        header_style="bold red",
    )
    table.add_column("Tipo", style="red")
    table.add_column("Categoria", style="cyan")
    table.add_column("Uso Atual", style="yellow")
    table.add_column("% Limite", style="white")

    for alert in alerts:
        table.add_row(
            alert.get("alert_type", "—"),
            alert.get("category", "—"),
            f"${float(alert.get('current_usage', 0)):.4f}",
            f"{alert.get('percentage', 0) * 100:.1f}%",
        )
    return table


def build_no_alerts_panel() -> Panel:
    """Green ``Panel`` for ``/cost alerts`` empty state — semantic is
    "no alerts is good", distinct from the yellow "no data" pattern."""
    return Panel(
        Text("Nenhum alerta de orçamento ativo.", style="green"),
        title="🔔 Alertas de Orçamento",
        border_style="green",
    )


def build_estimate_panel(
    *, provider: str, model: str, estimate: Mapping[str, Any]
) -> Group:
    """Render the ``/cost estimate`` view (table + details panel)."""
    table = Table(
        title="💰 Estimativa de Custo",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Componente", style="white")
    table.add_column("Tokens", style="yellow")
    table.add_column("Custo", style="green")

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
    return Group(table, "", details)
