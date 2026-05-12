"""ModelCommand — multi-provider model management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...config.manager import CommandConfig
from ...core.interfaces.selector import (InteractiveSelector,
                                         SelectorNotSupported, SelectorOption)
from ...core.models.tier_router import get_tier_router, reset_tier_router
from ...storage.usage_repository import BudgetGuard, get_usage_repository
from ..base import CommandContext, CommandResult, DirectCommand
from ._model_views import (build_budget_panel, build_cost_table,
                           build_current_panel, build_list_table)
from ._shared import error_panel, get_agent, split_args

logger = logging.getLogger(__name__)

_MODEL_PROVIDERS_YAML: Path = Path(__file__).parents[2] / "config" / "model_providers.yaml"
"""Caminho canônico do catalog YAML — antes computado 4x em-método com
import local de ``Path``, todos resolvendo para o mesmo arquivo."""


class ModelCommand(DirectCommand):
    """Multi-provider model management command."""

    # ModelCommand owns FOUR CLI flags (issue #126). cli_flag holds the
    # canonical one (--model is already handled separately for FORCE-model
    # syntax in cli.py); the other three sub-flags are declared via the
    # `cli_extra_flags` dict consumed by the CLI builder.
    cli_flag = None  # primary "--model PROVIDER:MODEL_ID" handled by cli.py
    cli_requires_provider = False
    cli_extra_flags = {
        "--model-list": {
            "subcommand": "list",
            "help": "List all available models in the catalog and exit.",
            "takes_arg": False,
            "requires_provider": False,
        },
        "--model-current": {
            "subcommand": "current",
            "help": "Show the currently active model and routing cascade.",
            "takes_arg": False,
            "requires_provider": False,
        },
        "--model-strategy": {
            "subcommand": "strategy",
            "help": "Switch routing strategy (task_optimized | cost_optimized).",
            "takes_arg": True,
            "metavar": "NAME",
            "requires_provider": False,
        },
        "--model-budget": {
            "subcommand": "budget",
            "help": "Show budget limits and consumption.",
            "takes_arg": False,
            "requires_provider": False,
        },
    }

    def __init__(self, selector: Optional[InteractiveSelector] = None) -> None:
        config = CommandConfig(
            name="model",
            description="Manage AI models — list, switch, show cost/budget",
        )
        super().__init__(config)
        self.category = "ai"
        self._selector = selector
        self.help_text = """
Model Command — Multi-Provider Management

USAGE:
    /model [list]                        List all models with pricing
    /model select                        Pick a model interactively (↑↓ Enter, ESC, type to filter)
    /model current                       Show active model + tier + cascade
    /model use <provider>:<model_id>     Force a specific model for this session
    /model use auto                      Return to automatic tier routing
    /model strategy <name>               Switch routing strategy (task_optimized | cost_optimized)
    /model cost                          Show accumulated cost for this session
    /model budget                        Show budget limits and consumption

EXAMPLES:
    /model list
    /model select
    /model use anthropic:claude-opus-4-7
    /model strategy cost_optimized
    /model cost
"""

    async def execute(self, context: CommandContext) -> CommandResult:
        args = split_args(context)
        action = args[0].lower() if args else "list"

        try:
            if action in ("list", ""):
                return await self._list(context)
            if action in ("select", "pick"):
                return await self._select(context)
            if action == "current":
                return await self._current(context)
            if action == "use":
                target = args[1] if len(args) > 1 else ""
                return await self._use(target, context)
            if action == "strategy":
                name = args[1] if len(args) > 1 else ""
                return await self._strategy(name, context)
            if action == "cost":
                return await self._cost(context)
            if action == "budget":
                return await self._budget(context)
            return CommandResult(
                success=False,
                content=Panel(
                    Text(f"Unknown sub-command '{action}'. See /model help.", style="red"),
                    title="Error",
                    border_style="red",
                ),
            )
        except Exception as exc:
            logger.error("ModelCommand error: %s", exc)
            return CommandResult(
                success=False,
                content=error_panel(str(exc), title="Error"),
            )

    # ------------------------------------------------------------------
    # /model list
    # ------------------------------------------------------------------

    async def _list(self, context: CommandContext) -> CommandResult:
        from deile.core.models.catalog import ModelCatalog

        catalog = ModelCatalog.from_yaml(_MODEL_PROVIDERS_YAML)
        handles = catalog.list_all()
        forced = self._get_forced(context)
        table = build_list_table(handles, forced)
        return CommandResult(success=True, content=table, metadata={"count": len(handles)})

    # ------------------------------------------------------------------
    # /model select
    # ------------------------------------------------------------------

    async def _select(self, context: CommandContext) -> CommandResult:
        from deile.core.models.catalog import ModelCatalog

        try:
            ctx_data = getattr(context.session, "context_data", {}) or {}
        except AttributeError:
            ctx_data = {}
        if ctx_data.get("model_override_locked"):
            locked_model = ctx_data.get("forced_model") or "(unknown)"
            return CommandResult(
                success=False,
                content=Panel(
                    Text(
                        "Model selection is locked by bot configuration.\n"
                        f"Forced model: {locked_model}",
                        style="yellow",
                    ),
                    title="[bold red]Model Override Locked[/bold red]",
                    border_style="red",
                ),
                metadata={"model_override_locked": True, "forced_model": locked_model},
            )

        selector = self._resolve_selector()

        catalog = ModelCatalog.from_yaml(_MODEL_PROVIDERS_YAML)
        handles = sorted(catalog.list_all(), key=lambda h: (h.tier.value, h.provider_id, h.model_id))
        forced = self._get_forced(context)

        options: List[SelectorOption] = []
        default_index = 0
        forced_found = False
        for idx, h in enumerate(handles):
            key = f"{h.provider_id}:{h.model_id}"
            label = f"{key}"
            if forced == key:
                default_index = idx
                forced_found = True
                label = f"{key}  (current)"
            description = (
                f"tier={h.tier.value}  in=${h.pricing.input_per_1m_usd:.2f}/1M  "
                f"out=${h.pricing.output_per_1m_usd:.2f}/1M  ctx={h.context_window // 1000}K"
            )
            options.append(SelectorOption(
                label=label,
                value=key,
                description=description,
                metadata={"provider": h.provider_id, "tier": h.tier.value},
            ))

        if not options:
            return CommandResult(
                success=False,
                content=Panel(
                    Text("No models available in catalog.", style="yellow"),
                    title="Model",
                    border_style="yellow",
                ),
            )

        if forced and not forced_found:
            prompt = (
                f"Select a model (previous '{forced}' is no longer in the catalog) "
                "— ↑↓ navigate, Enter confirm, ESC cancel, type to filter"
            )
        else:
            prompt = "Select a model (↑↓ navigate, Enter confirm, ESC cancel, type to filter)"

        try:
            choice = await selector.select(
                options,
                prompt=prompt,
                default_index=default_index,
            )
        except SelectorNotSupported:
            return await self._list_with_fallback_hint(context)

        if choice is None:
            return CommandResult(
                success=True,
                content=Panel(
                    Text("Selection cancelled.", style="dim"),
                    title="Model",
                    border_style="yellow",
                ),
                metadata={"cancelled": True},
            )

        return await self._use(str(choice.value), context)

    def _resolve_selector(self) -> InteractiveSelector:
        if self._selector is not None:
            return self._selector
        from deile.infrastructure.selectors import get_default_selector
        return get_default_selector()

    async def _list_with_fallback_hint(self, context: CommandContext) -> CommandResult:
        result = await self._list(context)
        result.metadata = {**(result.metadata or {}), "interactive_unavailable": True}
        if isinstance(result.content, Table):
            result.content.caption = (
                "[dim yellow]Interactive picker unavailable (no TTY) — "
                "use /model use <provider>:<model_id> to switch.[/dim yellow]"
            )
        return result

    # ------------------------------------------------------------------
    # /model current
    # ------------------------------------------------------------------

    async def _current(self, context: CommandContext) -> CommandResult:
        from deile.core.models.tier_router import get_tier_router

        forced = self._get_forced(context)
        try:
            router = get_tier_router()
            policy = router.policy()
            tier_1_cascade = policy.cascade_for_tier(__import__(
                "deile.core.models.tier", fromlist=["ModelTier"]
            ).ModelTier.TIER_1)
        except Exception:
            tier_1_cascade = []

        return CommandResult(
            success=True,
            content=build_current_panel(forced, tier_1_cascade),
            metadata={"forced": forced},
        )

    # ------------------------------------------------------------------
    # /model use <provider:model_id> | auto
    # ------------------------------------------------------------------

    async def _use(self, target: str, context: CommandContext) -> CommandResult:
        if not target:
            return CommandResult(
                success=False,
                content=Panel(
                    Text("Usage: /model use <provider>:<model_id>  or  /model use auto", style="yellow"),
                    title="Missing argument",
                    border_style="yellow",
                ),
            )

        try:
            ctx_data = getattr(context.session, "context_data", {}) or {}
        except AttributeError:
            ctx_data = {}
        if ctx_data.get("model_override_locked"):
            locked_model = ctx_data.get("forced_model") or "(unknown)"
            return CommandResult(
                success=False,
                content=Panel(
                    Text(
                        "Model selection is locked by bot configuration.\n"
                        f"Forced model: {locked_model}\n"
                        "Changing it with /model use is not allowed in this session.",
                        style="yellow",
                    ),
                    title="[bold red]Model Override Locked[/bold red]",
                    border_style="red",
                ),
                metadata={"model_override_locked": True, "forced_model": locked_model},
            )

        if target.lower() == "auto":
            if hasattr(context, "session") and context.session is not None:
                ctx_data.pop("forced_model", None)
            return CommandResult(
                success=True,
                content=Panel(Text("Routing restored to automatic."), title="Model", border_style="green"),
            )

        if ":" not in target:
            return CommandResult(
                success=False,
                content=Panel(
                    Text(f"Invalid format '{target}'. Use provider:model_id.", style="red"),
                    title="Error",
                    border_style="red",
                ),
            )

        # Validate against registered providers BEFORE accepting the override —
        # otherwise we'd return a green "OK" panel and only fail on the next message.
        # We only validate when context.agent is a *real* agent with a real providers
        # dict; MagicMock-based test contexts skip validation (they don't bootstrap
        # any providers anyway).
        forced_provider_id, forced_model_id = target.split(":", 1)
        agent_obj = get_agent(context)
        registered: Optional[dict] = None
        if agent_obj is not None and hasattr(agent_obj, "model_router"):
            providers_attr = getattr(agent_obj.model_router, "providers", None)
            if isinstance(providers_attr, dict):
                registered = providers_attr
        if registered is not None:
            exact_match = any(
                getattr(p, "provider_id", None) == forced_provider_id
                and getattr(p, "model_name", None) == forced_model_id
                for p in registered.values()
            )
            if not exact_match:
                available = sorted({
                    f"{getattr(p, 'provider_id', '?')}:{getattr(p, 'model_name', '?')}"
                    for p in registered.values()
                    if getattr(p, "provider_id", None) == forced_provider_id
                })
                return CommandResult(
                    success=False,
                    content=Panel(
                        Text(
                            f"Model '{target}' is not registered.\n"
                            f"Available {forced_provider_id} models: "
                            f"{available or '(none — provider not registered)'}",
                            style="yellow",
                        ),
                        title="[bold red]Forced Model Not Registered[/bold red]",
                        border_style="red",
                        subtitle="Use /model list to see all options",
                    ),
                )

        if hasattr(context, "session") and context.session is not None:
            if not hasattr(context.session, "context_data") or context.session.context_data is None:
                context.session.context_data = {}
            context.session.context_data["forced_model"] = target
        return CommandResult(
            success=True,
            content=Panel(
                Text(f"Model forced to {target} for this session."),
                title="Model",
                border_style="green",
            ),
            metadata={"forced_model": target},
        )

    # ------------------------------------------------------------------
    # /model strategy <name>
    # ------------------------------------------------------------------

    async def _strategy(self, name: str, context: CommandContext) -> CommandResult:
        valid = {"task_optimized", "cost_optimized"}
        if name not in valid:
            return CommandResult(
                success=False,
                content=Panel(
                    Text(f"Unknown strategy '{name}'. Valid: {', '.join(sorted(valid))}", style="red"),
                    title="Error",
                    border_style="red",
                ),
            )

        # Capture providers from the OLD TierRouter so we can re-register on the new one
        old_providers: List[Any] = []
        try:
            old_router = get_tier_router()
            old_providers = list(old_router.registered_providers().values())
        except Exception:
            old_providers = []

        reset_tier_router()
        new_router = get_tier_router(yaml_path=_MODEL_PROVIDERS_YAML, policy_name=name)

        # Re-register every provider that was on the old router so cascade resolution still works
        for p in old_providers:
            try:
                new_router.register_provider(p)
            except Exception:
                pass

        # Sync the legacy ModelRouter.strategy (consulted when tier classification fails)
        try:
            from deile.core.models.router import RoutingStrategy as _RS
            agent_obj = get_agent(context)
            if agent_obj is None:
                # Fall back: try common aliases on the context
                agent_obj = getattr(context, "deile_agent", None)
            if agent_obj is not None and hasattr(agent_obj, "model_router"):
                agent_obj.model_router.strategy = _RS(name)
        except Exception as exc:
            logger.debug("could not sync legacy ModelRouter.strategy: %s", exc)

        return CommandResult(
            success=True,
            content=Panel(Text(f"Strategy set to '{name}'."), title="Strategy", border_style="green"),
            metadata={"strategy": name},
        )

    # ------------------------------------------------------------------
    # /model cost
    # ------------------------------------------------------------------

    async def _cost(self, context: CommandContext) -> CommandResult:
        repo = get_usage_repository()
        session_id = self._session_id(context)
        total = repo.cost_for_session(session_id)
        records = repo.records_for_session(session_id)
        table = build_cost_table(records, total, session_id)
        return CommandResult(success=True, content=table, metadata={"total_cost_usd": total})

    # ------------------------------------------------------------------
    # /model budget
    # ------------------------------------------------------------------

    async def _budget(self, context: CommandContext) -> CommandResult:
        repo = get_usage_repository()
        try:
            guard = BudgetGuard.from_yaml(_MODEL_PROVIDERS_YAML, repo)
        except Exception:
            return CommandResult(
                success=False,
                content=Panel(Text("Could not load budget from YAML."), title="Budget", border_style="yellow"),
            )

        session_id = self._session_id(context)
        session_spent = repo.cost_for_session(session_id)
        return CommandResult(
            success=True,
            content=build_budget_panel(guard.snapshot(), session_spent),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_forced(context: CommandContext) -> Optional[str]:
        try:
            return context.session.context_data.get("forced_model")
        except AttributeError:
            return None

    @staticmethod
    def _session_id(context: CommandContext) -> str:
        try:
            return context.session.session_id
        except AttributeError:
            return "default"
