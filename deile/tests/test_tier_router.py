"""Tests: RoutingPolicy + CircuitBreaker + TierRouter — Phase 7."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from deile.core.models.tier import ModelTier
from deile.core.models.tier_router import (BreakerState, CircuitBreaker,
                                           NoProviderAvailable, RoutingPolicy,
                                           TierRouter)

# Path to the real YAML (used in integration-style tests)
_YAML_PATH = Path(__file__).parents[2] / "deile" / "config" / "model_providers.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(provider_id: str) -> MagicMock:
    p = MagicMock()
    p.provider_id = provider_id
    p.provider_name = provider_id
    return p


# ---------------------------------------------------------------------------
# RoutingPolicy tests
# ---------------------------------------------------------------------------

class TestRoutingPolicy:
    def test_from_yaml_loads_task_optimized(self):
        policy = RoutingPolicy.from_yaml(_YAML_PATH, "task_optimized")
        assert policy.name == "task_optimized"

    def test_from_yaml_loads_cost_optimized(self):
        policy = RoutingPolicy.from_yaml(_YAML_PATH, "cost_optimized")
        assert policy.name == "cost_optimized"

    def test_task_optimized_has_four_tiers(self):
        policy = RoutingPolicy.from_yaml(_YAML_PATH, "task_optimized")
        assert len(policy.available_tiers()) == 4

    def test_tier_1_cascade_non_empty(self):
        policy = RoutingPolicy.from_yaml(_YAML_PATH, "task_optimized")
        cascade = policy.cascade_for_tier(ModelTier.TIER_1)
        assert len(cascade) >= 1

    def test_cascade_entries_have_provider_colon_model_format(self):
        policy = RoutingPolicy.from_yaml(_YAML_PATH, "task_optimized")
        for tier in ModelTier:
            for entry in policy.cascade_for_tier(tier):
                assert ":" in entry, f"Entry {entry!r} missing ':'"

    def test_unknown_policy_raises_key_error(self):
        with pytest.raises(KeyError):
            RoutingPolicy.from_yaml(_YAML_PATH, "nonexistent_policy")

    def test_cascade_for_unknown_tier_returns_empty(self):
        _policy = RoutingPolicy.from_yaml(_YAML_PATH, "task_optimized")
        # Build a custom policy with only tier_1 to test missing tier
        from deile.core.models.tier_router import RoutingPolicy as RP
        p = RP("test", {ModelTier.TIER_1: ["a:b"]})
        assert p.cascade_for_tier(ModelTier.TIER_4) == []

    def test_cost_optimized_tier_4_has_cheaper_providers(self):
        """Cost-optimized tier_4 should NOT include Anthropic (expensive)."""
        policy = RoutingPolicy.from_yaml(_YAML_PATH, "cost_optimized")
        cascade = policy.cascade_for_tier(ModelTier.TIER_4)
        provider_ids = [entry.split(":")[0] for entry in cascade]
        assert "anthropic" not in provider_ids


# ---------------------------------------------------------------------------
# CircuitBreaker tests
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_initially_closed(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        assert cb.get_state("p1") == BreakerState.CLOSED
        assert cb.allow_request("p1") is True

    def test_trips_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        cb.record_failure("p1")
        cb.record_failure("p1")
        assert cb.get_state("p1") == BreakerState.CLOSED
        cb.record_failure("p1")  # 3rd failure → OPEN
        assert cb.get_state("p1") == BreakerState.OPEN

    def test_open_circuit_blocks_requests(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb.record_failure("p1")
        cb.record_failure("p1")
        assert cb.allow_request("p1") is False
        assert cb.is_open("p1") is True

    def test_success_resets_counter(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("p1")
        cb.record_failure("p1")
        cb.record_success("p1")
        # Counter reset; two more failures needed to trip
        cb.record_failure("p1")
        cb.record_failure("p1")
        assert cb.get_state("p1") == BreakerState.CLOSED

    def test_transitions_to_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.01)
        cb.record_failure("p1")
        assert cb.get_state("p1") == BreakerState.OPEN
        time.sleep(0.02)
        # allow_request triggers OPEN → HALF_OPEN transition
        assert cb.allow_request("p1") is True
        assert cb.get_state("p1") == BreakerState.HALF_OPEN

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.01)
        cb.record_failure("p1")
        time.sleep(0.02)
        cb.allow_request("p1")  # → HALF_OPEN
        cb.record_success("p1")
        assert cb.get_state("p1") == BreakerState.CLOSED

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.01)
        cb.record_failure("p1")
        time.sleep(0.02)
        cb.allow_request("p1")  # → HALF_OPEN
        cb.record_failure("p1")  # → OPEN again
        assert cb.get_state("p1") == BreakerState.OPEN

    def test_reset_force_closes(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure("p1")
        assert cb.get_state("p1") == BreakerState.OPEN
        cb.reset("p1")
        assert cb.get_state("p1") == BreakerState.CLOSED

    def test_independent_breakers_per_provider(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure("p1")
        cb.record_failure("p1")
        # p1 is OPEN, p2 is still CLOSED
        assert cb.is_open("p1") is True
        assert cb.is_open("p2") is False


# ---------------------------------------------------------------------------
# TierRouter tests
# ---------------------------------------------------------------------------

class TestTierRouter:
    def _make_router(self, cascade: list, cb: CircuitBreaker = None):
        policy = RoutingPolicy("test", {
            ModelTier.TIER_1: cascade,
            ModelTier.TIER_2: cascade,
        })
        catalog = MagicMock()
        cb = cb or CircuitBreaker()
        return TierRouter(catalog, policy, cb)

    def test_select_returns_registered_provider(self):
        router = self._make_router(["anthropic:claude-opus-4-7"])
        router.register_provider(_make_provider("anthropic"))

        provider = router.select(ModelTier.TIER_1)
        assert provider.provider_id == "anthropic"

    def test_select_skips_unregistered_provider(self):
        router = self._make_router(["openai:gpt-4o", "anthropic:claude-opus-4-7"])
        router.register_provider(_make_provider("anthropic"))  # openai not registered

        provider = router.select(ModelTier.TIER_1)
        assert provider.provider_id == "anthropic"

    def test_select_skips_tripped_circuit(self):
        cb = CircuitBreaker(failure_threshold=1)
        router = self._make_router(["openai:gpt-4o", "anthropic:claude-opus-4-7"], cb)
        router.register_provider(_make_provider("openai"))
        router.register_provider(_make_provider("anthropic"))

        cb.record_failure("openai")  # trips openai
        provider = router.select(ModelTier.TIER_1)
        assert provider.provider_id == "anthropic"

    def test_select_raises_when_all_unavailable(self):
        router = self._make_router(["openai:gpt-4o"])
        # No providers registered
        with pytest.raises(NoProviderAvailable):
            router.select(ModelTier.TIER_1)

    def test_select_raises_for_unconfigured_tier(self):
        policy = RoutingPolicy("test", {ModelTier.TIER_1: ["a:b"]})
        catalog = MagicMock()
        cb = CircuitBreaker()
        router = TierRouter(catalog, policy, cb)
        with pytest.raises(NoProviderAvailable):
            router.select(ModelTier.TIER_4)

    def test_record_failure_trips_circuit_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        router = self._make_router(["openai:gpt-4o", "anthropic:claude-opus-4-7"], cb)
        router.register_provider(_make_provider("openai"))
        router.register_provider(_make_provider("anthropic"))

        router.record_failure("openai")
        router.record_failure("openai")
        router.record_failure("openai")  # trips

        provider = router.select(ModelTier.TIER_1)
        assert provider.provider_id == "anthropic"

    def test_record_success_resets_circuit(self):
        cb = CircuitBreaker(failure_threshold=1)
        router = self._make_router(["openai:gpt-4o", "anthropic:claude-opus-4-7"], cb)
        router.register_provider(_make_provider("openai"))
        router.register_provider(_make_provider("anthropic"))

        router.record_failure("openai")
        assert router.select(ModelTier.TIER_1).provider_id == "anthropic"

        router.record_success("openai")
        assert router.select(ModelTier.TIER_1).provider_id == "openai"

    def test_registered_providers_reflects_state(self):
        router = self._make_router(["a:m1"])
        p = _make_provider("a")
        router.register_provider(p)
        assert "a" in router.registered_providers()

    def test_unregister_provider(self):
        router = self._make_router(["a:m1"])
        router.register_provider(_make_provider("a"))
        router.unregister_provider("a")
        with pytest.raises(NoProviderAvailable):
            router.select(ModelTier.TIER_1)

    def test_first_in_cascade_preferred(self):
        """TierRouter must return the FIRST available provider, not any provider."""
        cb = CircuitBreaker()
        router = self._make_router(["anthropic:claude-opus-4-7", "openai:gpt-4o"], cb)
        router.register_provider(_make_provider("anthropic"))
        router.register_provider(_make_provider("openai"))

        assert router.select(ModelTier.TIER_1).provider_id == "anthropic"
