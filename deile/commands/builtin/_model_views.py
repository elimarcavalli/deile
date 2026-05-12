"""Pure Rich-rendering helpers for ``/model`` subcommands.

Extracted from :class:`ModelCommand` to keep the command focused on
dispatch + side effects (router mutation, repo reads, YAML loading) while
the visual layer becomes independently testable. Same separation already
applied to ``status_command`` + ``_status_collectors.py``.

Each helper takes plain data — no router, repo, agent or context — and
returns a Rich renderable. The caller is responsible for fetching that
data from the right subsystem.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence

from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def build_list_table(handles: Iterable[Any], forced: Optional[str]) -> Table:
    """Render the ``/model list`` table.

    ``handles`` is a sequence of ``ModelHandle`` (with ``provider_id``,
    ``model_id``, ``tier``, ``pricing``, ``context_window``, ``capabilities``).
    ``forced`` is the active ``"provider:model_id"`` key or ``None``.
    """
    table = Table(title="Available Models", show_header=True, header_style="bold cyan")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Model ID", no_wrap=True)
    table.add_column("Tier", justify="center")
    table.add_column("In $/1M", justify="right")
    table.add_column("Out $/1M", justify="right")
    table.add_column("Context", justify="right")
    table.add_column("Capabilities")
    table.add_column("Active", justify="center")

    for h in sorted(handles, key=lambda x: (x.tier.value, x.provider_id)):
        key = f"{h.provider_id}:{h.model_id}"
        active = "✓" if forced and forced == key else ""
        caps = ", ".join(sorted(h.capabilities))
        table.add_row(
            h.provider_id,
            h.model_id,
            h.tier.value,
            f"${h.pricing.input_per_1m_usd:.2f}",
            f"${h.pricing.output_per_1m_usd:.2f}",
            f"{h.context_window // 1000}K",
            caps,
            active,
        )
    return table


def build_current_panel(forced: Optional[str], tier_1_cascade: Sequence[str]) -> Panel:
    """Render the ``/model current`` panel."""
    lines = []
    if forced:
        lines.append(f"Forced model : {forced}")
    else:
        lines.append("Routing      : automatic (tier-based)")
    lines.append(f"Tier-1 cascade: {', '.join(tier_1_cascade) or 'not configured'}")
    return Panel(Text("\n".join(lines)), title="Current Routing", border_style="green")


def build_cost_table(records: Iterable[Any], total: float, session_id: str) -> Table:
    """Render the ``/model cost`` table.

    ``records`` is an iterable of ``UsageRecord`` (with ``provider_id``,
    ``model_id``, ``prompt_tokens``, ``completion_tokens``, ``cost_usd``).
    The TOTAL row carries the precomputed ``total`` so the caller can
    fetch it from the repo in one shot.
    """
    table = Table(title=f"Session Cost  (session={session_id})", header_style="bold")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Calls", justify="right")
    table.add_column("In tokens", justify="right")
    table.add_column("Out tokens", justify="right")
    table.add_column("Cost $", justify="right")

    agg: dict = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
    n_records = 0
    for r in records:
        n_records += 1
        key = (r.provider_id, r.model_id)
        agg[key]["calls"] += 1
        agg[key]["in"] += r.prompt_tokens
        agg[key]["out"] += r.completion_tokens
        agg[key]["cost"] += r.cost_usd

    for (provider, model), vals in sorted(agg.items()):
        table.add_row(
            provider,
            model,
            str(vals["calls"]),
            str(vals["in"]),
            str(vals["out"]),
            f"${vals['cost']:.4f}",
        )

    table.add_section()
    table.add_row("TOTAL", "", str(n_records), "", "", f"${total:.4f}")
    return table


def build_budget_panel(snap: Mapping[str, Any], session_spent: float) -> Panel:
    """Render the ``/model budget`` panel."""
    per_session = snap["per_session_usd"]
    lines = [
        f"Per-session limit : ${per_session:.2f}",
        f"Session spent     : ${session_spent:.4f}",
        f"Remaining         : ${max(0.0, per_session - session_spent):.4f}",
        "",
        f"Guard enabled     : {snap['enabled']}",
    ]
    if snap["per_provider_daily_usd"]:
        lines.append("")
        lines.append("Daily limits:")
        for pid, limit in snap["per_provider_daily_usd"].items():
            lines.append(f"  {pid}: ${limit:.2f}")
    if snap["per_provider_monthly_usd"]:
        lines.append("")
        lines.append("Monthly limits:")
        for pid, limit in snap["per_provider_monthly_usd"].items():
            lines.append(f"  {pid}: ${limit:.2f}")
    lines.append("")
    lines.append(f"Alert threshold   : {snap['alert_threshold_pct']}%")
    return Panel(Text("\n".join(lines)), title="Budget", border_style="blue")
