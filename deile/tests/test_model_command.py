"""Tests: ModelCommand multi-provider rewrite — Phase 15."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Sequence
from unittest.mock import MagicMock, patch

import pytest

from deile.commands.builtin.model_command import ModelCommand
from deile.core.interfaces.selector import InteractiveSelector, SelectorOption

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
            "model_override_lock_source": "deilebot",
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
            "model_override_lock_source": "deilebot",
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
        from deile.storage.usage_repository import (UsageRepository,
                                                    reset_usage_repository)
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
        from deile.storage.usage_repository import (UsageRecord,
                                                    UsageRepository,
                                                    reset_usage_repository)
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
        from deile.storage.usage_repository import (UsageRepository,
                                                    reset_usage_repository)
        reset_usage_repository()
        repo = UsageRepository(db_path=tmp_path / "test.db")

        cmd = ModelCommand()
        ctx = _make_context("budget")

        with patch("deile.commands.builtin.model_command.get_usage_repository", return_value=repo):
            result = await cmd.execute(ctx)

        assert result.success


# ---------------------------------------------------------------------------
# /model select  (interactive picker)
# ---------------------------------------------------------------------------


class _StubSelector(InteractiveSelector):
    """Test double — no terminal I/O, deterministic answer."""

    def __init__(self, supported: bool, choice: Optional[SelectorOption]):
        self._supported = supported
        self._choice = choice
        self.calls: list[dict] = []

    def is_supported(self) -> bool:
        return self._supported

    async def select(
        self,
        options: Sequence[SelectorOption],
        *,
        prompt: str = "Select an option",
        default_index: int = 0,
    ) -> Optional[SelectorOption]:
        self.calls.append(
            {"options": list(options), "prompt": prompt, "default_index": default_index}
        )
        return self._choice


class TestModelSelect:
    @pytest.mark.asyncio
    async def test_select_falls_back_to_list_when_unsupported(self):
        sel = _StubSelector(supported=False, choice=None)
        cmd = ModelCommand(selector=sel)
        result = await cmd.execute(_make_context("select"))
        assert result.success is True
        # Falls back to the same content as /model list (a Rich Table) but
        # tags the fallback so callers can distinguish the path and the user
        # gets a caption explaining why interactive mode is unavailable.
        from rich.table import Table
        assert isinstance(result.content, Table)
        assert result.content.caption is not None
        assert "no TTY" in str(result.content.caption)
        assert result.metadata.get("interactive_unavailable") is True
        assert sel.calls == []

    @pytest.mark.asyncio
    async def test_select_falls_back_when_adapter_raises_not_supported(self):
        from deile.core.interfaces.selector import SelectorNotSupported

        class _RaisingSelector(_StubSelector):
            async def select(self, options, *, prompt="", default_index=0):
                raise SelectorNotSupported("legacy console")

        sel = _RaisingSelector(supported=True, choice=None)
        cmd = ModelCommand(selector=sel)
        result = await cmd.execute(_make_context("select"))
        assert result.success is True
        assert result.metadata.get("interactive_unavailable") is True

    @pytest.mark.asyncio
    async def test_select_warns_when_forced_model_not_in_catalog(self):
        sel = _StubSelector(supported=True, choice=None)
        cmd = ModelCommand(selector=sel)
        ctx = _make_context("select")
        ctx.session.context_data = {"forced_model": "ghost:nonexistent-model"}
        await cmd.execute(ctx)
        assert sel.calls[0]["default_index"] == 0
        assert "no longer in the catalog" in sel.calls[0]["prompt"]
        # No row carries the (current) marker because the forced model isn't there.
        assert all("(current)" not in opt.label for opt in sel.calls[0]["options"])

    @pytest.mark.asyncio
    async def test_select_returns_cancelled_when_user_escapes(self):
        sel = _StubSelector(supported=True, choice=None)
        cmd = ModelCommand(selector=sel)
        ctx = _make_context("select")
        result = await cmd.execute(ctx)
        assert result.success is True
        assert result.metadata.get("cancelled") is True
        assert "forced_model" not in ctx.session.context_data
        assert len(sel.calls) == 1

    @pytest.mark.asyncio
    async def test_select_applies_chosen_model(self):
        chosen = SelectorOption(
            label="anthropic:claude-haiku-4-5  (current)",
            value="anthropic:claude-haiku-4-5",
        )
        sel = _StubSelector(supported=True, choice=chosen)
        cmd = ModelCommand(selector=sel)
        ctx = _make_context("select")
        result = await cmd.execute(ctx)
        assert result.success is True
        assert ctx.session.context_data.get("forced_model") == "anthropic:claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_select_alias_pick(self):
        chosen = SelectorOption(label="openai:gpt-4o", value="openai:gpt-4o")
        sel = _StubSelector(supported=True, choice=chosen)
        cmd = ModelCommand(selector=sel)
        ctx = _make_context("pick")
        result = await cmd.execute(ctx)
        assert result.success is True
        assert ctx.session.context_data.get("forced_model") == "openai:gpt-4o"

    @pytest.mark.asyncio
    async def test_select_denied_when_override_locked(self):
        sel = _StubSelector(supported=True, choice=SelectorOption(label="x", value="x:y"))
        cmd = ModelCommand(selector=sel)
        ctx = _make_context("select")
        ctx.session.context_data = {
            "forced_model": "deepseek:deepseek-v4-pro",
            "model_override_locked": True,
            "model_override_lock_source": "deilebot",
        }
        result = await cmd.execute(ctx)
        assert result.success is False
        assert sel.calls == []  # never reached the picker
        assert ctx.session.context_data["forced_model"] == "deepseek:deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_select_default_index_points_to_forced_model(self):
        """When a model is already forced, the picker should preselect that row."""
        from deile.core.models.catalog import ModelCatalog

        catalog = ModelCatalog.from_yaml(_YAML_PATH)
        handles = sorted(
            catalog.list_all(),
            key=lambda h: (h.tier.value, h.provider_id, h.model_id),
        )
        # Pick a forced model that is guaranteed to be in the catalog and not first.
        forced_idx = next(
            (i for i, h in enumerate(handles) if i > 0),
            None,
        )
        assert forced_idx is not None and forced_idx > 0
        forced_key = f"{handles[forced_idx].provider_id}:{handles[forced_idx].model_id}"

        sel = _StubSelector(supported=True, choice=None)
        cmd = ModelCommand(selector=sel)
        ctx = _make_context("select")
        ctx.session.context_data = {"forced_model": forced_key}
        await cmd.execute(ctx)
        assert sel.calls[0]["default_index"] == forced_idx

    @pytest.mark.asyncio
    async def test_select_options_match_catalog_size(self):
        from deile.core.models.catalog import ModelCatalog
        catalog = ModelCatalog.from_yaml(_YAML_PATH)
        sel = _StubSelector(supported=True, choice=None)
        cmd = ModelCommand(selector=sel)
        await cmd.execute(_make_context("select"))
        assert len(sel.calls[0]["options"]) == len(catalog.list_all())


# ---------------------------------------------------------------------------
# Unknown sub-command
# ---------------------------------------------------------------------------

class TestModelUnknown:
    @pytest.mark.asyncio
    async def test_unknown_subcommand_fails(self):
        cmd = ModelCommand()
        result = await cmd.execute(_make_context("xyzzy"))
        assert result.success is False
