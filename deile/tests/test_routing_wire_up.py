"""Tests: routing strategy wire-up — Phase 10."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.core.models.tier import ModelTier
from deile.core.models.tier_router import (
    CircuitBreaker,
    RoutingPolicy,
    TierRouter,
    get_tier_router,
    reset_tier_router,
)

_YAML_PATH = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"


def _make_provider(provider_id: str):
    p = MagicMock()
    p.provider_id = provider_id
    p.provider_name = provider_id
    return p


# ---------------------------------------------------------------------------
# get_tier_router() factory
# ---------------------------------------------------------------------------

class TestGetTierRouterFactory:
    def setup_method(self):
        reset_tier_router()

    def teardown_method(self):
        reset_tier_router()

    def test_factory_loads_from_default_yaml(self):
        router = get_tier_router()
        assert isinstance(router, TierRouter)

    def test_factory_returns_singleton(self):
        r1 = get_tier_router()
        r2 = get_tier_router()
        assert r1 is r2

    def test_force_new_creates_fresh_instance(self):
        r1 = get_tier_router()
        r2 = get_tier_router(force_new=True)
        assert r1 is not r2

    def test_factory_with_explicit_yaml_path(self):
        router = get_tier_router(yaml_path=_YAML_PATH, force_new=True)
        assert isinstance(router, TierRouter)

    def test_factory_with_cost_optimized_policy(self):
        router = get_tier_router(
            yaml_path=_YAML_PATH, policy_name="cost_optimized", force_new=True
        )
        assert router.policy().name == "cost_optimized"

    def test_factory_tier1_cascade_non_empty(self):
        router = get_tier_router(force_new=True)
        cascade = router.policy().cascade_for_tier(ModelTier.TIER_1)
        assert len(cascade) >= 1

    def test_factory_circuit_breaker_is_configured(self):
        router = get_tier_router(force_new=True)
        cb = router.circuit_breaker()
        assert isinstance(cb, CircuitBreaker)
        # Threshold from YAML is 3; verify it trips on 3 failures
        cb.record_failure("test_p")
        cb.record_failure("test_p")
        assert not cb.is_open("test_p")
        cb.record_failure("test_p")
        assert cb.is_open("test_p")


# ---------------------------------------------------------------------------
# ModelRouter.select_provider() tier delegation
# ---------------------------------------------------------------------------

class TestModelRouterTierDelegation:
    @pytest.mark.asyncio
    async def test_select_provider_delegates_to_tier_router_when_tier_given(self):
        from deile.core.models.router import ModelRouter

        router = ModelRouter()
        mock_provider = _make_provider("anthropic")

        # Build a TierRouter that returns mock_provider for TIER_1
        policy = RoutingPolicy("test", {ModelTier.TIER_1: ["anthropic:claude-opus-4-7"]})
        cb = CircuitBreaker()
        tier_router = TierRouter(MagicMock(), policy, cb)
        tier_router.register_provider(mock_provider)

        with patch("deile.core.models.router.get_tier_router", return_value=tier_router):
            selected = await router.select_provider(tier=ModelTier.TIER_1)

        assert selected.provider_id == "anthropic"

    @pytest.mark.asyncio
    async def test_select_provider_falls_back_to_legacy_when_no_tier(self):
        from deile.core.models.router import ModelRouter

        router = ModelRouter()
        legacy_provider = _make_provider("gemini")
        router.register_provider(legacy_provider)

        # Without tier kwarg, legacy routing is used (no TierRouter call)
        with patch("deile.core.models.router.get_tier_router") as mock_factory:
            selected = await router.select_provider()  # no tier=

        mock_factory.assert_not_called()
        assert selected.provider_id == "gemini"

    @pytest.mark.asyncio
    async def test_select_provider_falls_back_when_tier_router_fails(self):
        from deile.core.models.router import ModelRouter

        router = ModelRouter()
        legacy_provider = _make_provider("gemini")
        router.register_provider(legacy_provider)

        with patch("deile.core.models.router.get_tier_router", side_effect=Exception("broken")):
            selected = await router.select_provider(tier=ModelTier.TIER_1)

        assert selected.provider_id == "gemini"

    @pytest.mark.asyncio
    async def test_tier_router_registers_legacy_providers_automatically(self):
        from deile.core.models.router import ModelRouter

        router = ModelRouter()
        openai_provider = _make_provider("openai")
        router.register_provider(openai_provider)

        policy = RoutingPolicy("test", {ModelTier.TIER_2: ["openai:gpt-4o"]})
        cb = CircuitBreaker()
        tier_router = TierRouter(MagicMock(), policy, cb)
        # openai NOT pre-registered in tier_router

        with patch("deile.core.models.router.get_tier_router", return_value=tier_router):
            selected = await router.select_provider(tier=ModelTier.TIER_2)

        # The router auto-registered openai from its own providers dict
        assert selected.provider_id == "openai"
