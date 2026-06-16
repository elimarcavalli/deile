"""TierRouter — tier-aware multi-provider router.

Implements three collaborating components:
- RoutingPolicy: loads tier→cascade mappings from model_providers.yaml
- CircuitBreaker: per-provider consecutive-failure counter with cooldown
- TierRouter: selects first healthy provider in a tier's cascade
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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
            tier_1: [anthropic:claude-opus-4-8, openai:gpt-4o, ...]
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
            raise KeyError(
                f"Policy '{policy_name}' not found in {path}. Available: {list(policies)}"
            )

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
    CLOSED = "closed"  # Normal — requests allowed
    OPEN = "open"  # Tripped — requests rejected
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
        """True if the circuit is currently blocking *provider_id*.

        Side-effect-free: must NOT transition OPEN→HALF_OPEN. The previous
        implementation delegated to ``allow_request``, which "consumes" the
        single HALF_OPEN probe slot when it flips the state. ``TierRouter.select``
        calls ``is_open`` once per cascade entry; with duplicate provider_ids
        in a tier (the YAML allows them: ``[gemini:..., gemini:..., ...]``),
        the first call would burn the probe before any cascade entry that
        actually targets that provider could grab it — leaving the breaker
        stuck OPEN for another full cooldown.
        """
        b = self._breakers.get(provider_id)
        if b is None or b.state == BreakerState.CLOSED:
            return False
        if b.state == BreakerState.HALF_OPEN:
            return False  # probe slot is available
        # OPEN: blocking unless the cooldown has elapsed (read-only check).
        return (time.time() - b.opened_at) < self._cooldown

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
        # Two indexes:
        #   _providers: full key "provider_id:model_id" → ModelProvider (cascade match)
        #   _providers_by_id: provider_id → ModelProvider (compat fallback when a
        #     cascade entry's model_id isn't registered, or when callers register
        #     a provider whose model_name isn't a meaningful string e.g. MagicMock)
        self._providers: Dict[str, ModelProvider] = {}
        self._providers_by_id: Dict[str, ModelProvider] = {}

    def register_provider(self, provider: ModelProvider) -> None:
        provider_id = provider.provider_id
        model_id = getattr(provider, "model_name", None)
        # Provider_id-only fallback: keep the FIRST registered (assume bootstrap registers
        # the flagship/most-capable model first) for deterministic fallback behavior.
        # If a test calls register_provider(MagicMock()) without a model_name, store unconditionally.
        if not isinstance(model_id, str) or not model_id:
            # Non-string model_name (e.g. MagicMock) — overwrite is fine since this is test-only
            self._providers_by_id[provider_id] = provider
            logger.debug("TierRouter: registered %s (no model_name)", provider_id)
            return
        if provider_id not in self._providers_by_id:
            self._providers_by_id[provider_id] = provider
        # Always store under the full key
        full_key = f"{provider_id}:{model_id}"
        self._providers[full_key] = provider
        logger.debug("TierRouter: registered %s", full_key)

    def unregister_provider(self, provider_id: str) -> None:
        # Remove the provider_id-only entry and any matching full-key entries
        self._providers_by_id.pop(provider_id, None)
        keys_to_drop = [k for k in self._providers if k.split(":", 1)[0] == provider_id]
        for k in keys_to_drop:
            self._providers.pop(k, None)

    def select(
        self,
        tier: ModelTier,
        skip_provider_ids: Optional[Iterable[str]] = None,
    ) -> ModelProvider:
        """Select the first available provider in the tier cascade.

        For each cascade entry "provider_id:model_id":
        - Skip if provider_id is in *skip_provider_ids* (in-request fallback).
        - Skip if circuit breaker for provider_id is open.
        - Look up the exact full-key match first; if found, return it.
        - Otherwise fall back to any registered provider with that provider_id.

        Args:
            tier: requested model tier
            skip_provider_ids: provider_ids to bypass even though their CB is closed
                (used by the agent's cascade retry to skip a provider that just
                failed within the current request, before the CB threshold trips).

        Raises NoProviderAvailable if nothing is usable.
        """
        cascade = self._policy.cascade_for_tier(tier)
        if not cascade:
            raise NoProviderAvailable(f"No cascade configured for tier={tier.value}")

        skip_set = set(skip_provider_ids or ())

        for key in cascade:
            provider_id = key.split(":", 1)[0]
            if provider_id in skip_set:
                logger.debug("TierRouter: skipping %s (in skip_provider_ids)", key)
                continue
            # Check breaker without side-effects first — duplicate provider_ids
            # in a cascade must not consume the HALF_OPEN probe slot before
            # we've found a registered provider to actually use it.
            if self._circuit_breaker.is_open(provider_id):
                logger.debug("TierRouter: skipping %s (circuit open)", key)
                continue
            # Prefer exact provider:model_id match (lets cascade pick the right cheap/expensive variant)
            if key in self._providers:
                # Commit: consume the breaker probe slot for the chosen
                # cascade entry only (side-effectful OPEN→HALF_OPEN transition).
                self._circuit_breaker.allow_request(provider_id)
                logger.debug("TierRouter: selected %s (exact match)", key)
                return self._providers[key]
            # Fall back to any registered instance for this provider
            if provider_id in self._providers_by_id:
                self._circuit_breaker.allow_request(provider_id)
                logger.debug(
                    "TierRouter: selected %s (provider_id fallback — exact model not registered)",
                    provider_id,
                )
                return self._providers_by_id[provider_id]
            logger.debug("TierRouter: skipping %s (not registered)", key)

        raise NoProviderAvailable(
            f"All providers exhausted for tier={tier.value}. "
            f"Cascade: {cascade}. "
            f"Skipped: {sorted(skip_set)}. "
            f"Registered: {list(self._providers) or list(self._providers_by_id)}."
        )

    def record_success(self, provider_id: str) -> None:
        self._circuit_breaker.record_success(provider_id)

    def record_failure(self, provider_id: str) -> None:
        self._circuit_breaker.record_failure(provider_id)

    def registered_providers(self) -> Dict[str, ModelProvider]:
        """Return the full-key index. For backward compatibility, when a provider
        was registered without a meaningful ``model_name`` (e.g. test mocks), the
        provider_id-only fallback dict is merged in so ``"anthropic" in registered_providers()``
        keeps working in older tests.
        """
        merged: Dict[str, ModelProvider] = {}
        merged.update(self._providers_by_id)  # provider_id → provider
        merged.update(self._providers)  # full key takes precedence
        return merged

    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    def policy(self) -> RoutingPolicy:
        return self._policy


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_tier_router_singleton: Optional[TierRouter] = None

_DEFAULT_YAML = Path(__file__).parents[2] / "config" / "model_providers.yaml"


def get_tier_router(
    yaml_path: Optional[Path] = None,
    *,
    policy_name: Optional[str] = None,
    force_new: bool = False,
) -> TierRouter:
    """Return the singleton TierRouter, bootstrapped from *yaml_path*.

    On first call (or when *force_new* is True) the factory:
    1. Loads ``ModelCatalog`` from the YAML.
    2. Reads ``default_strategy`` (or uses *policy_name*) to pick a ``RoutingPolicy``.
    3. Reads ``circuit_breaker`` config to build a ``CircuitBreaker``.
    4. Constructs and returns a ``TierRouter``.

    Providers must still be registered explicitly via ``router.register_provider()``.
    """
    global _tier_router_singleton

    if _tier_router_singleton is not None and not force_new:
        return _tier_router_singleton

    path = yaml_path or _DEFAULT_YAML

    with open(path) as f:
        data = yaml.safe_load(f)

    # Strategy / policy name
    resolved_policy = policy_name or data.get("default_strategy", "task_optimized")

    catalog = ModelCatalog.from_yaml(path)
    policy = RoutingPolicy.from_yaml(path, resolved_policy)

    cb_cfg = data.get("circuit_breaker", {})
    circuit_breaker = CircuitBreaker(
        failure_threshold=int(cb_cfg.get("consecutive_failures_threshold", 3)),
        cooldown_seconds=float(cb_cfg.get("cooldown_seconds", 60.0)),
    )

    _tier_router_singleton = TierRouter(catalog, policy, circuit_breaker)
    logger.info(
        "TierRouter bootstrapped: policy=%s, models=%d",
        resolved_policy,
        len(catalog.list_all()),
    )
    return _tier_router_singleton


def reset_tier_router() -> None:
    """Reset the singleton (test helper)."""
    global _tier_router_singleton
    _tier_router_singleton = None
