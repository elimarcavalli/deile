"""Testes do comando /cost — bugs de runtime e subcomandos reais"""

from decimal import Decimal
from io import StringIO
from typing import Dict, List
from unittest.mock import MagicMock, patch

import pytest

from deile.commands.base import CommandContext
from deile.commands.builtin.cost_command import CostCommand


def _render_rich(obj) -> str:
    from rich.console import Console

    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=False, width=200)
    console.print(obj)
    return buf.getvalue()


def _make_context(args: str = "", session_id: str = "test") -> CommandContext:
    session = MagicMock()
    session.session_id = session_id
    ctx = CommandContext(user_input=f"/cost {args}", args=args)
    ctx.session = session
    return ctx


def _make_summary(
    total: str = "0",
    entry_count: int = 0,
    categories: Dict = None,
    top_expenses: List[Dict] = None,
):
    s = MagicMock()
    s.total_amount = Decimal(total)
    s.entry_count = entry_count
    s.categories = categories or {}
    s.top_expenses = top_expenses or []
    return s


# ---------------------------------------------------------------------------
# Bug fixes
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_summary_empty_database_no_crash():
    cmd = CostCommand()
    empty_summary = _make_summary()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=empty_summary):
        with patch.object(
            cmd.cost_tracker, "get_current_session_cost", return_value=Decimal("0")
        ):
            result = await cmd.execute(_make_context("summary"))
    assert result.success


@pytest.mark.unit
async def test_summary_populated_database_shows_real_data():
    cats = {"api_calls": Decimal("1.23"), "compute": Decimal("0.45")}
    summary = _make_summary(total="1.68", entry_count=10, categories=cats)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        with patch.object(
            cmd.cost_tracker, "get_current_session_cost", return_value=Decimal("0.1")
        ):
            result = await cmd.execute(_make_context("summary"))
    assert result.success
    rendered = _render_rich(result.content)
    assert "10" in rendered or "1.68" in rendered


@pytest.mark.unit
async def test_decimal_formatting_no_exception():
    summary = _make_summary(total="1.234567890")
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        with patch.object(
            cmd.cost_tracker, "get_current_session_cost", return_value=Decimal("0")
        ):
            result = await cmd.execute(_make_context("summary"))
    assert result.success


@pytest.mark.unit
async def test_version_from_version_module(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from deile.__version__ import __version__

    export_data = '{"entries": [{"id": "1", "amount": 0.01}], "total_entries": 1}'
    summary = _make_summary(total="0.01", entry_count=1)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        with patch.object(cmd.cost_tracker, "export_costs", return_value=export_data):
            result = await cmd.execute(_make_context("export json"))
    assert result.success
    rendered = _render_rich(result.content)
    assert "4.0.0" not in rendered, "Versão hardcoded 4.0.0 encontrada"
    assert __version__ in rendered


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_categories_from_real_database():
    cats = {"api_calls": Decimal("2.0"), "model_usage": Decimal("1.0")}
    summary = _make_summary(total="3.0", categories=cats)

    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        result = await cmd.execute(_make_context("categories"))
    assert result.success
    rendered = _render_rich(result.content)
    assert "api_calls" in rendered or "model_usage" in rendered


@pytest.mark.unit
async def test_categories_empty_database_no_crash():
    summary = _make_summary()
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        result = await cmd.execute(_make_context("categories"))
    assert result.success


@pytest.mark.unit
async def test_budget_list_reads_real_limits():
    budget = MagicMock()
    budget.category = "api_calls"
    budget.period = "monthly"
    budget.limit_amount = Decimal("100")
    budget.alert_threshold = 0.8
    budget.hard_limit = False

    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "_load_budget_limits"):
        cmd.cost_tracker.budget_limits = {"api_calls_monthly": budget}
        result = await cmd.execute(_make_context("budget list"))
    assert result.success
    rendered = _render_rich(result.content)
    assert "api_calls" in rendered


@pytest.mark.unit
async def test_budget_list_empty_no_crash():
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "_load_budget_limits"):
        cmd.cost_tracker.budget_limits = {}
        result = await cmd.execute(_make_context("budget list"))
    assert result.success


@pytest.mark.unit
async def test_budget_set_persists():
    cmd = CostCommand()
    with patch.object(
        cmd.cost_tracker, "set_budget_limit", return_value=True
    ) as mock_set:
        result = await cmd.execute(_make_context("budget set api_calls monthly 50"))
    assert result.success
    mock_set.assert_called_once_with("api_calls", "monthly", 50.0)


@pytest.mark.unit
async def test_budget_set_invalid_amount_returns_error():
    cmd = CostCommand()
    result = await cmd.execute(_make_context("budget set api_calls monthly abc"))
    assert not result.success


@pytest.mark.unit
async def test_top_n_returns_most_expensive():
    top = [
        {
            "category": "api_calls",
            "subcategory": "gpt4",
            "amount": 0.5,
            "description": "call",
        },
        {
            "category": "compute",
            "subcategory": "gpu",
            "amount": 0.3,
            "description": "run",
        },
    ]
    summary = _make_summary(total="0.8", entry_count=2, top_expenses=top)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        result = await cmd.execute(_make_context("top 2"))
    assert result.success
    rendered = _render_rich(result.content)
    assert "api_calls" in rendered


@pytest.mark.unit
async def test_top_empty_no_crash():
    summary = _make_summary()
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        result = await cmd.execute(_make_context("top 5"))
    assert result.success


@pytest.mark.unit
async def test_export_json_writes_valid_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    export_data = (
        '{"entries": [{"id": "1", "amount": 0.01}], '
        '"period_start": "2026-01-01", "period_end": "2026-01-31"}'
    )
    summary = _make_summary(total="0.01", entry_count=1)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        with patch.object(cmd.cost_tracker, "export_costs", return_value=export_data):
            result = await cmd.execute(_make_context("export json 30"))
    assert result.success
    json_files = list(tmp_path.glob("costs_export_*.json"))
    assert len(json_files) == 1
    import json

    data = json.loads(json_files[0].read_text())
    assert "entries" in data


@pytest.mark.unit
async def test_export_json_no_entries_skips_file_creation(tmp_path, monkeypatch):
    """Issue #301: don't create dead `{"entries": []}` files when there's
    no data to export."""
    monkeypatch.chdir(tmp_path)
    summary = _make_summary(entry_count=0)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        # export_costs should NOT be called when entry_count is 0 — gate is
        # the summary check, not the payload check.
        with patch.object(cmd.cost_tracker, "export_costs") as mock_export:
            result = await cmd.execute(_make_context("export json 30"))
    assert result.success
    json_files = list(tmp_path.glob("costs_export_*.json"))
    assert len(json_files) == 0, f"Expected no files, got {json_files}"
    mock_export.assert_not_called()


@pytest.mark.unit
async def test_export_csv_no_entries_skips_file_creation(tmp_path, monkeypatch):
    """Issue #301: o gate de entry_count também precisa cobrir CSV.

    CSV é particularmente sutil porque ``cost_tracker.export_costs`` sempre
    retorna pelo menos a linha de header — string truthy que não seria
    capturada por um ``if not data:``.
    """
    monkeypatch.chdir(tmp_path)
    summary = _make_summary(entry_count=0)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        with patch.object(cmd.cost_tracker, "export_costs") as mock_export:
            result = await cmd.execute(_make_context("export csv 30"))
    assert result.success
    csv_files = list(tmp_path.glob("costs_export_*.csv"))
    assert csv_files == [], f"Esperado nenhum arquivo CSV, achei {csv_files}"
    mock_export.assert_not_called()


@pytest.mark.unit
async def test_export_csv_writes_valid_file_when_data_exists(tmp_path, monkeypatch):
    """Happy-path do export CSV: simetria com ``test_export_json_writes_valid_file``.

    Antes deste teste, só o caminho JSON-com-dados tinha cobertura; o CSV
    feliz era exercitado apenas indiretamente por ``test_all_subcommands_dispatched``.
    """
    monkeypatch.chdir(tmp_path)
    csv_payload = (
        "id,datetime,category,subcategory,amount,currency,description,session_id\r\n"
        "1,2026-01-01T00:00:00,api_calls,gpt4,0.5,USD,call,sess1\r\n"
    )
    summary = _make_summary(total="0.5", entry_count=1)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        with patch.object(cmd.cost_tracker, "export_costs", return_value=csv_payload):
            result = await cmd.execute(_make_context("export csv 30"))
    assert result.success
    csv_files = list(tmp_path.glob("costs_export_*.csv"))
    assert len(csv_files) == 1
    content = csv_files[0].read_text(encoding="utf-8")
    assert "api_calls" in content and "0.5" in content


@pytest.mark.unit
async def test_export_summary_query_failure_does_not_write_file(tmp_path, monkeypatch):
    """Resiliência: se ``get_cost_summary`` levantar (DB lock, corrupção,
    permissão), o comando precisa falhar limpo e NÃO vazar arquivo no CWD.

    Cobre o branch ``except Exception`` de ``_export_costs`` no caminho onde
    o ``summary`` é consultado antes do export.
    """
    monkeypatch.chdir(tmp_path)
    cmd = CostCommand()
    with patch.object(
        cmd.cost_tracker,
        "get_cost_summary",
        side_effect=RuntimeError("simulação de falha no DB"),
    ):
        result = await cmd.execute(_make_context("export json 30"))
    assert not result.success
    leaked = list(tmp_path.glob("costs_export_*"))
    assert leaked == [], f"Falha no DB vazou arquivo: {leaked}"


@pytest.mark.unit
async def test_forecast_insufficient_data_message():
    summary = _make_summary(total="0", entry_count=0)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        result = await cmd.execute(_make_context("forecast"))
    assert result.success
    rendered = _render_rich(result.content)
    assert (
        "insuficiente" in rendered.lower()
        or "insufficient" in rendered.lower()
        or "Dados" in rendered
    )


@pytest.mark.unit
async def test_forecast_with_data_returns_projection():
    summary = _make_summary(total="30.0", entry_count=100)
    cmd = CostCommand()
    with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
        result = await cmd.execute(_make_context("forecast 14"))
    assert result.success
    rendered = _render_rich(result.content)
    assert "14" in rendered


@pytest.mark.unit
async def test_alerts_no_crash_when_empty():
    cmd = CostCommand()
    cmd.cost_tracker.cost_alerts = []
    result = await cmd.execute(_make_context("alerts"))
    assert result.success


@pytest.mark.unit
async def test_all_subcommands_dispatched(tmp_path, monkeypatch):
    """Nenhum subcomando declarado deve retornar 'Ação desconhecida'"""
    monkeypatch.chdir(tmp_path)
    cmd = CostCommand()
    declared = [
        ("summary", {}),
        ("session", {}),
        ("categories", {}),
        ("budget list", {}),
        ("forecast", {}),
        ("export json", {"export_costs": '{"entries": []}'}),
        ("estimate gemini pro 100", {}),
        ("top 5", {}),
        ("alerts", {}),
    ]
    for args, extra_patches in declared:
        ctx = _make_context(args)
        # entry_count>0 so /cost export does not short-circuit on the
        # "no data" guard (issue #301) and we exercise the full dispatch.
        summary = _make_summary(total="0.01", entry_count=1)
        with patch.object(cmd.cost_tracker, "get_cost_summary", return_value=summary):
            with patch.object(
                cmd.cost_tracker, "get_current_session_cost", return_value=Decimal("0")
            ):
                with patch.object(
                    cmd.cost_tracker,
                    "export_costs",
                    return_value=extra_patches.get("export_costs", "{}"),
                ):
                    with patch.object(cmd.cost_tracker, "_load_budget_limits"):
                        cmd.cost_tracker.budget_limits = {}
                        cmd.cost_tracker.cost_alerts = []
                        with patch.object(
                            cmd.cost_tracker,
                            "get_pricing_estimate",
                            return_value={"error": "no pricing"},
                        ):
                            result = await cmd.execute(ctx)
        rendered = _render_rich(result.content) if result.content else ""
        assert (
            "desconhecida" not in rendered.lower()
        ), f"Subcomando '{args}' sem dispatch real"


@pytest.mark.unit
async def test_unknown_action_returns_error():
    cmd = CostCommand()
    result = await cmd.execute(_make_context("nonexistent_action"))
    assert not result.success
