"""TierRouter — tier-aware multi-provider router.

Implements three collaborating components:
- RoutingPolicy: loads tier→cascade mappings from model_providers.yaml
- CircuitBreaker: per-provider consecutive-failure counter with cooldown
- TierRouter: selects first healthy provider in a tier's cascade
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from deile.core.models.base import ModelProvider
from deile.core.models.catalog import ModelCatalog
from deile.core.models.tier import ModelTier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RoutingPolicy
# ---------------------------------------------------------------------------

class RoutingPolicy:
    """Maps each ModelTier to an ordered cascade of 'provider_id:model_id' keys.

    Loaded from the ``policies`` section of model_providers.yaml:

        policies:
          task_optimized:
            tier_1: [anthropic:claude-opus-4-7, openai:gpt-4o, ...]
          cost_optimized:
            tier_1: [deepseek:deepseek-chat, openai:gpt-4o-mini, ...]
    """

    def __init__(self, name: str, cascades: Dict[ModelTier, List[str]]) -> None:
        self.name = name
        self._cascades = cascades

    @classmethod
    def from_yaml(cls, path: Path, policy_name: str) -> "RoutingPolicy":
        """Load a named policy from a model_providers.yaml file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        policies = data.get("policies", {})
        if policy_name not in policies:
            raise KeyError(f"Policy '{policy_name}' not found in {path}. Available: {list(policies)}")

        tier_map = policies[policy_name]
        cascades: Dict[ModelTier, List[str]] = {}

        tier_key_map = {
            "tier_1": ModelTier.TIER_1,
            "tier_2": ModelTier.TIER_2,
            "tier_3": ModelTier.TIER_3,
            "tier_4": ModelTier.TIER_4,
        }
        for yaml_key, tier in tier_key_map.items():
            if yaml_key in tier_map:
                cascades[tier] = list(tier_map[yaml_key])

        return cls(name=policy_name, cascades=cascades)

    def cascade_for_tier(self, tier: ModelTier) -> List[str]:
        """Return ordered list of 'provider_id:model_id' for *tier*."""
        return list(self._cascades.get(tier, []))

    def available_tiers(self) -> List[ModelTier]:
        return list(self._cascades.keys())


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class BreakerState(Enum):
    CLOSED = "closed"        # Normal — requests allowed
    OPEN = "open"            # Tripped — requests rejected
    HALF_OPEN = "half_open"  # Cooldown elapsed — one probe allowed


@dataclass
class _ProviderBreaker:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """Per-provider circuit breaker with consecutive-failure threshold and cooldown.

    States:
        CLOSED  → request succeeds  → stay CLOSED (reset consecutive_failures)
        CLOSED  → request fails     → increment; if ≥ threshold → OPEN
        OPEN    → cooldown elapsed  → HALF_OPEN (one probe allowed)
        HALF_OPEN → probe succeeds  → CLOSED
        HALF_OPEN → probe fails     → OPEN (restart cooldown)
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._breakers: Dict[str, _ProviderBreaker] = {}

    def _get(self, provider_id: str) -> _ProviderBreaker:
        if provider_id not in self._breakers:
            self._breakers[provider_id] = _ProviderBreaker()
        return self._breakers[provider_id]

    def allow_request(self, provider_id: str) -> bool:
        """Return True if a request to *provider_id* should be attempted."""
        b = self._get(provider_id)
        if b.state == BreakerState.CLOSED:
            return True
        if b.state == BreakerState.OPEN:
            elapsed = time.time() - b.opened_at
            if elapsed >= self._cooldown:
                b.state = BreakerState.HALF_OPEN
                return True  # One probe
            return False
        # HALF_OPEN — allow one request
        return True

    def is_open(self, provider_id: str) -> bool:
        """True if the circuit is currently blocking *provider_id*."""
        return not self.allow_request(provider_id)

    def record_success(self, provider_id: str) -> None:
        b = self._get(provider_id)
        b.consecutive_failures = 0
        if b.state in (BreakerState.OPEN, BreakerState.HALF_OPEN):
            logger.info("CircuitBreaker: %s recovered → CLOSED", provider_id)
        b.state = BreakerState.CLOSED

    def record_failure(self, provider_id: str) -> None:
        b = self._get(provider_id)
        b.consecutive_failures += 1
        if b.consecutive_failures >= self._threshold:
            if b.state != BreakerState.OPEN:
                logger.warning(
                    "CircuitBreaker: %s tripped after %d failures → OPEN",
                    provider_id,
                    b.consecutive_failures,
                )
            b.state = BreakerState.OPEN
            b.opened_at = time.time()

    def get_state(self, provider_id: str) -> BreakerState:
        """Return the current state without side-effects (does NOT transition OPEN→HALF_OPEN)."""
        return self._get(provider_id).state

    def reset(self, provider_id: str) -> None:
        """Force-reset a provider's breaker to CLOSED (e.g., after manual recovery)."""
        self._breakers[provider_id] = _ProviderBreaker()


# ---------------------------------------------------------------------------
# NoProviderAvailable
# ---------------------------------------------------------------------------

class NoProviderAvailable(Exception):
    """Raised when all cascade entries are tripped or unregistered."""


# ---------------------------------------------------------------------------
# TierRouter
# ---------------------------------------------------------------------------

class TierRouter:
    """Tier-aware router: iterates a tier's cascade, skips tripped breakers,
    returns the first registered provider that can serve the request.

    Usage:
        router = TierRouter(catalog, policy, circuit_breaker)
        router.register_provider(anthropic_provider)
        router.register_provider(openai_provider)
        provider = router.select(ModelTier.TIER_1)
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        policy: RoutingPolicy,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self._catalog = catalog
        self._policy = policy
        self._circuit_breaker = circuit_breaker
        # provider_id → ModelProvider (last registration wins)
        self._providers: Dict[str, ModelProvider] = {}

    def register_provider(self, provider: ModelProvider) -> None:
        self._providers[provider.provider_id] = provider
        logger.debug("TierRouter: registered provider_id=%s", provider.provider_id)

    def unregister_provider(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def select(self, tier: ModelTier) -> ModelProvider:
        """Select the first available provider in the tier cascade.

        Skips entries whose:
        - circuit breaker is open
        - provider_id is not registered

        Raises NoProviderAvailable if nothing is usable.
        """
        cascade = self._policy.cascade_for_tier(tier)
        if not cascade:
            raise NoProviderAvailable(f"No cascade configured for tier={tier.value}")

        for key in cascade:
            provider_id = key.split(":")[0]
            if self._circuit_breaker.is_open(provider_id):
                logger.debug("TierRouter: skipping %s (circuit open)", provider_id)
                continue
            if provider_id not in self._providers:
                logger.debug("TierRouter: skipping %s (not registered)", provider_id)
                continue
            return self._providers[provider_id]

        raise NoProviderAvailable(
            f"All providers exhausted for tier={tier.value}. "
            f"Cascade: {cascade}. "
            f"Registered: {list(self._providers)}."
        )

    def record_success(self, provider_id: str) -> None:
        self._circuit_breaker.record_success(provider_id)

    def record_failure(self, provider_id: str) -> None:
        self._circuit_breaker.record_failure(provider_id)

    def registered_providers(self) -> Dict[str, ModelProvider]:
        return dict(self._providers)

    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    def policy(self) -> RoutingPolicy:
        return self._policy
