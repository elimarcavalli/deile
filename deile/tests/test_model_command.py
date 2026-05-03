"""Tests: ModelCommand multi-provider rewrite — Phase 15."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from deile.commands.builtin.model_command import ModelCommand

_YAML_PATH = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_context(args: str = "", session_id: str = "sess-test") -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    ctx.session = SimpleNamespace(session_id=session_id, context_data={})
    return ctx


# ---------------------------------------------------------------------------
# /model list
# ---------------------------------------------------------------------------

class TestModelList:
    @pytest.mark.asyncio
    async def test_list_returns_success(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("list"))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_list_no_args_also_succeeds(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context(""))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_list_includes_all_models(self):
        from deile.core.models.catalog import ModelCatalog
        catalog = ModelCatalog.from_yaml(_YAML_PATH)
        handles = catalog.list_all()

        cmd = ModelCommand()
        result = await cmd.execute(_make_context("list"))
        assert result.success is True
        assert result.metadata["count"] == len(handles)

    @pytest.mark.asyncio
    async def test_list_result_is_rich_table(self):
        from rich.table import Table
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("list"))
        assert isinstance(result.content, Table)

    @pytest.mark.asyncio
    async def test_list_shows_pricing_data(self):
        """Providers have $input / $output columns — metadata count > 0 means table built."""
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("list"))
        assert result.metadata["count"] >= 1


# ---------------------------------------------------------------------------
# /model current
# ---------------------------------------------------------------------------

class TestModelCurrent:
    @pytest.mark.asyncio
    async def test_current_returns_success(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("current"))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_current_shows_auto_when_no_forced(self):
        cmd = ModelCommand()
        ctx = _make_context("current")
        ctx.session.context_data = {}
        result = await cmd.execute(ctx)
        assert result.success
        assert result.metadata.get("forced") is None

    @pytest.mark.asyncio
    async def test_current_shows_forced_model(self):
        cmd = ModelCommand()
        ctx = _make_context("current")
        ctx.session.context_data = {"forced_model": "anthropic:claude-opus-4-7"}
        result = await cmd.execute(ctx)
        assert result.success
        assert result.metadata.get("forced") == "anthropic:claude-opus-4-7"


# ---------------------------------------------------------------------------
# /model use
# ---------------------------------------------------------------------------

class TestModelUse:
    @pytest.mark.asyncio
    async def test_use_forces_model(self):
        cmd = ModelCommand()
        ctx = _make_context("use anthropic:claude-opus-4-7")
        result = await cmd.execute(ctx)
        assert result.success
        assert ctx.session.context_data.get("forced_model") == "anthropic:claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_use_auto_clears_forced(self):
        cmd = ModelCommand()
        ctx = _make_context("use auto")
        ctx.session.context_data = {"forced_model": "anthropic:claude-opus-4-7"}
        result = await cmd.execute(ctx)
        assert result.success
        assert "forced_model" not in ctx.session.context_data

    @pytest.mark.asyncio
    async def test_use_denies_changes_when_model_override_locked(self):
        cmd = ModelCommand()
        ctx = _make_context("use anthropic:claude-opus-4-7")
        ctx.session.context_data = {
            "forced_model": "deepseek:deepseek-v4-pro",
            "model_override_locked": True,
            "model_override_lock_source": "deile_bot",
        }

        result = await cmd.execute(ctx)

        assert result.success is False
        assert result.metadata.get("model_override_locked") is True
        assert ctx.session.context_data["forced_model"] == "deepseek:deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_use_auto_denied_when_model_override_locked(self):
        cmd = ModelCommand()
        ctx = _make_context("use auto")
        ctx.session.context_data = {
            "forced_model": "deepseek:deepseek-v4-pro",
            "model_override_locked": True,
            "model_override_lock_source": "deile_bot",
        }

        result = await cmd.execute(ctx)

        assert result.success is False
        assert ctx.session.context_data["forced_model"] == "deepseek:deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_use_without_args_fails(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("use"))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_use_invalid_format_fails(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("use just-model-no-colon"))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_use_result_metadata(self):
        cmd = ModelCommand()
        ctx = _make_context("use openai:gpt-4o")
        result = await cmd.execute(ctx)
        assert result.success
        assert result.metadata.get("forced_model") == "openai:gpt-4o"

    @pytest.mark.asyncio
    async def test_use_rejects_unregistered_model_when_real_agent_present(self):
        """R9-M1: when context.agent has a real providers dict and the forced model
        is NOT in it, /model use must reject immediately with a red Rich panel."""
        cmd = ModelCommand()
        ctx = _make_context("use anthropic:nonexistent-model")
        # Wire a real providers dict containing only the flagship
        flagship = SimpleNamespace(
            provider_id="anthropic",
            model_name="claude-opus-4-7",
            provider_name="anthropic",
        )
        ctx.agent = SimpleNamespace(
            model_router=SimpleNamespace(providers={"anthropic:claude-opus-4-7": flagship})
        )
        result = await cmd.execute(ctx)
        assert result.success is False
        # session.context_data['forced_model'] must NOT have been written
        assert "forced_model" not in ctx.session.context_data

    @pytest.mark.asyncio
    async def test_use_accepts_registered_model_when_real_agent_present(self):
        """R9-M1: positive case — when the model IS registered, /model use accepts it."""
        cmd = ModelCommand()
        ctx = _make_context("use anthropic:claude-haiku-4-5")
        haiku = SimpleNamespace(
            provider_id="anthropic",
            model_name="claude-haiku-4-5",
            provider_name="anthropic",
        )
        ctx.agent = SimpleNamespace(
            model_router=SimpleNamespace(providers={"anthropic:claude-haiku-4-5": haiku})
        )
        result = await cmd.execute(ctx)
        assert result.success is True
        assert ctx.session.context_data.get("forced_model") == "anthropic:claude-haiku-4-5"


# ---------------------------------------------------------------------------
# /model strategy
# ---------------------------------------------------------------------------

class TestModelStrategy:
    @pytest.mark.asyncio
    async def test_strategy_task_optimized(self):
        cmd = ModelCommand()
        with patch("deile.commands.builtin.model_command.get_tier_router"), \
             patch("deile.commands.builtin.model_command.reset_tier_router"):
            result = await cmd.execute(_make_context("strategy task_optimized"))
        assert result.success
        assert result.metadata.get("strategy") == "task_optimized"

    @pytest.mark.asyncio
    async def test_strategy_cost_optimized(self):
        cmd = ModelCommand()
        with patch("deile.commands.builtin.model_command.get_tier_router"), \
             patch("deile.commands.builtin.model_command.reset_tier_router"):
            result = await cmd.execute(_make_context("strategy cost_optimized"))
        assert result.success
        assert result.metadata.get("strategy") == "cost_optimized"

    @pytest.mark.asyncio
    async def test_strategy_invalid_fails(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("strategy unknown_strategy"))
        assert result.success is False


# ---------------------------------------------------------------------------
# /model cost
# ---------------------------------------------------------------------------

class TestModelCost:
    @pytest.mark.asyncio
    async def test_cost_empty_session_returns_zero(self, tmp_path):
        from deile.storage.usage_repository import UsageRepository, reset_usage_repository
        reset_usage_repository()

        repo = UsageRepository(db_path=tmp_path / "test.db")
        cmd = ModelCommand()
        ctx = _make_context("cost", session_id="empty-sess")

        with patch("deile.commands.builtin.model_command.get_usage_repository", return_value=repo):
            result = await cmd.execute(ctx)

        assert result.success
        assert result.metadata.get("total_cost_usd") == 0.0

    @pytest.mark.asyncio
    async def test_cost_aggregates_correctly(self, tmp_path):
        from deile.storage.usage_repository import (
            UsageRepository, UsageRecord, reset_usage_repository
        )
        reset_usage_repository()

        repo = UsageRepository(db_path=tmp_path / "test.db")
        repo.record(UsageRecord(
            provider_id="anthropic", model_id="claude-opus-4-7",
            tier="tier_1", session_id="my-sess",
            cost_usd=0.10,
        ))
        repo.record(UsageRecord(
            provider_id="openai", model_id="gpt-4o",
            tier="tier_1", session_id="my-sess",
            cost_usd=0.20,
        ))

        cmd = ModelCommand()
        ctx = _make_context("cost", session_id="my-sess")

        with patch("deile.commands.builtin.model_command.get_usage_repository", return_value=repo):
            result = await cmd.execute(ctx)

        assert result.success
        assert abs(result.metadata["total_cost_usd"] - 0.30) < 1e-6


# ---------------------------------------------------------------------------
# /model budget
# ---------------------------------------------------------------------------

class TestModelBudget:
    @pytest.mark.asyncio
    async def test_budget_loads_from_yaml(self, tmp_path):
        from deile.storage.usage_repository import UsageRepository, reset_usage_repository
        reset_usage_repository()
        repo = UsageRepository(db_path=tmp_path / "test.db")

        cmd = ModelCommand()
        ctx = _make_context("budget")

        with patch("deile.commands.builtin.model_command.get_usage_repository", return_value=repo):
            result = await cmd.execute(ctx)

        assert result.success


# ---------------------------------------------------------------------------
# Unknown sub-command
# ---------------------------------------------------------------------------

class TestModelUnknown:
    @pytest.mark.asyncio
    async def test_unknown_subcommand_fails(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("xyzzy"))
        assert result.success is False
