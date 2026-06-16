"""Round-5 H1 lock: TierRouter must select the EXACT cascade model, not flagship."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deile.core.models.tier import ModelTier
from deile.core.models.tier_router import (
    CircuitBreaker,
    NoProviderAvailable,
    RoutingPolicy,
    TierRouter,
)


def _make_provider(provider_id: str, model_name: str) -> MagicMock:
    p = MagicMock()
    p.provider_id = provider_id
    p.model_name = model_name
    p.provider_name = provider_id
    return p


class TestTierRouterExactCascadeMatch:
    def test_tier3_picks_haiku_not_opus_when_both_registered(self):
        """If a cascade entry asks for `anthropic:claude-haiku-4-5` AND that exact
        instance is registered, it must be picked over a different anthropic instance.
        """
        policy = RoutingPolicy(
            "test",
            {
                ModelTier.TIER_3: ["anthropic:claude-haiku-4-5"],
            },
        )
        cb = CircuitBreaker()
        catalog = MagicMock()
        router = TierRouter(catalog, policy, cb)

        opus = _make_provider("anthropic", "claude-opus-4-8")
        haiku = _make_provider("anthropic", "claude-haiku-4-5")
        router.register_provider(opus)
        router.register_provider(haiku)

        selected = router.select(ModelTier.TIER_3)
        assert selected is haiku, "expected the haiku instance, got something else"
        assert selected.model_name == "claude-haiku-4-5"

    def test_tier1_picks_opus_when_both_registered(self):
        """Symmetric: the opus instance must be picked for tier_1 cascade."""
        policy = RoutingPolicy(
            "test",
            {
                ModelTier.TIER_1: ["anthropic:claude-opus-4-8"],
            },
        )
        cb = CircuitBreaker()
        router = TierRouter(MagicMock(), policy, cb)

        opus = _make_provider("anthropic", "claude-opus-4-8")
        haiku = _make_provider("anthropic", "claude-haiku-4-5")
        router.register_provider(opus)
        router.register_provider(haiku)

        selected = router.select(ModelTier.TIER_1)
        assert selected is opus
        assert selected.model_name == "claude-opus-4-8"

    def test_falls_back_to_provider_id_when_model_not_registered(self):
        """If cascade asks for `anthropic:nonexistent` but provider has another model,
        we should still get the registered anthropic instance (resilience)."""
        policy = RoutingPolicy(
            "test",
            {
                ModelTier.TIER_3: ["anthropic:nonexistent-model"],
            },
        )
        cb = CircuitBreaker()
        router = TierRouter(MagicMock(), policy, cb)

        haiku = _make_provider("anthropic", "claude-haiku-4-5")
        router.register_provider(haiku)

        selected = router.select(ModelTier.TIER_3)
        assert selected is haiku, "expected provider_id-fallback to give us haiku"

    def test_circuit_breaker_keys_by_provider_id_not_model_id(self):
        """Failures on opus should also block haiku since both are 'anthropic'."""
        policy = RoutingPolicy(
            "test",
            {
                ModelTier.TIER_3: ["anthropic:claude-haiku-4-5", "openai:gpt-4o"],
            },
        )
        cb = CircuitBreaker(failure_threshold=1)
        router = TierRouter(MagicMock(), policy, cb)

        haiku = _make_provider("anthropic", "claude-haiku-4-5")
        oai = _make_provider("openai", "gpt-4o")
        router.register_provider(haiku)
        router.register_provider(oai)

        # Trip anthropic's CB
        cb.record_failure("anthropic")
        selected = router.select(ModelTier.TIER_3)
        assert selected is oai, "anthropic's CB should be open → fall to openai"

    def test_unregister_drops_all_anthropic_instances(self):
        policy = RoutingPolicy(
            "test", {ModelTier.TIER_3: ["anthropic:claude-haiku-4-5"]}
        )
        router = TierRouter(MagicMock(), policy, CircuitBreaker())
        router.register_provider(_make_provider("anthropic", "claude-opus-4-8"))
        router.register_provider(_make_provider("anthropic", "claude-haiku-4-5"))
        router.unregister_provider("anthropic")
        with pytest.raises(NoProviderAvailable):
            router.select(ModelTier.TIER_3)

    def test_registered_providers_includes_full_keys(self):
        router = TierRouter(MagicMock(), RoutingPolicy("test", {}), CircuitBreaker())
        router.register_provider(_make_provider("anthropic", "claude-opus-4-8"))
        router.register_provider(_make_provider("anthropic", "claude-haiku-4-5"))
        registered = router.registered_providers()
        # Should expose both full keys for debugging / introspection
        assert "anthropic:claude-opus-4-8" in registered
        assert "anthropic:claude-haiku-4-5" in registered
