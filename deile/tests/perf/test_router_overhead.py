"""Performance: router select_provider overhead < 50ms per call — Phase 17."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from deile.core.models.tier import ModelTier
from deile.core.models.tier_router import (CircuitBreaker, RoutingPolicy,
                                           TierRouter, reset_tier_router)


def _make_mock_provider(provider_id: str) -> MagicMock:
    p = MagicMock()
    p.provider_id = provider_id
    p.provider_name = provider_id
    return p


@pytest.fixture(autouse=True)
def _reset():
    reset_tier_router()
    yield
    reset_tier_router()


def test_router_select_1000_calls_under_50ms():
    """1000 calls to TierRouter.select(TIER_2) must complete in < 50ms total."""
    policy = RoutingPolicy(
        "perf_test",
        {ModelTier.TIER_2: ["openai:gpt-4o", "anthropic:claude-haiku"]},
    )
    cb = CircuitBreaker()
    router = TierRouter(MagicMock(), policy, cb)
    router.register_provider(_make_mock_provider("openai"))
    router.register_provider(_make_mock_provider("anthropic"))

    N = 1000
    start = time.perf_counter()
    for _ in range(N):
        router.select(ModelTier.TIER_2)
    elapsed = time.perf_counter() - start

    per_call_ms = (elapsed / N) * 1000
    assert per_call_ms < 50, f"Router overhead {per_call_ms:.2f}ms/call exceeds 50ms limit"


def test_router_select_average_under_1ms():
    """Average select latency should be well under 1ms (target: sub-50ms RNF)."""
    policy = RoutingPolicy(
        "perf_test",
        {ModelTier.TIER_1: ["anthropic:claude-opus-4-8", "openai:gpt-4o"]},
    )
    cb = CircuitBreaker()
    router = TierRouter(MagicMock(), policy, cb)
    router.register_provider(_make_mock_provider("anthropic"))
    router.register_provider(_make_mock_provider("openai"))

    times = []
    for _ in range(200):
        t0 = time.perf_counter()
        router.select(ModelTier.TIER_1)
        times.append(time.perf_counter() - t0)

    avg_ms = (sum(times) / len(times)) * 1000
    assert avg_ms < 1.0, f"Average router select {avg_ms:.3f}ms exceeds 1ms"


def test_schema_translation_overhead():
    """ToolSchema.to_anthropic_tool() on 10 schemas must be fast."""
    from deile.tools.base import ToolSchema

    schemas = []
    for i in range(10):
        s = ToolSchema(
            name=f"tool_{i}",
            description=f"Tool number {i}",
            parameters={
                "type": "object",
                "properties": {
                    "arg": {"type": "string", "description": "an arg"},
                },
                "required": ["arg"],
            },
        )
        schemas.append(s)

    start = time.perf_counter()
    for s in schemas:
        s.to_anthropic_tool()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 10, f"Schema translation {elapsed_ms:.2f}ms for 10 schemas too slow"
