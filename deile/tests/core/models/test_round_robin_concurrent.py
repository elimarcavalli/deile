"""Regression test for round-robin under concurrent selection.

Bug: ``_round_robin_selection`` did
    selected = providers[idx % N]; idx += 1
which is NOT atomic. Two concurrent ``select_provider`` calls (e.g.
``asyncio.gather`` of independent pipeline workers) could both read
the same ``idx`` before either wrote — double-picking the same
provider. Fairness was lost; in pathological cases the rotation never
fully cycled.

Fix: use ``itertools.count`` whose ``next()`` is atomic under the GIL.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from deile.core.models.routing_strategies import (RoutingContext,
                                                  RoutingStrategy,
                                                  RoutingStrategySelector)


def _provider(name: str) -> MagicMock:
    p = MagicMock()
    p.provider_name = name
    p.model_name = "m"
    return p


async def test_round_robin_fair_under_concurrent_calls() -> None:
    """N concurrent picks across K providers must distribute fairly."""
    selector = RoutingStrategySelector()
    providers = [_provider(f"p{i}") for i in range(4)]
    metrics = {f"p{i}:m": MagicMock(active_requests=0) for i in range(4)}
    ctx = RoutingContext(user_input="x", task_type="t", priority="normal")

    # 40 picks via gather = 10 each provider in a fair rotation.
    async def one_pick():
        return selector.select(
            RoutingStrategy.ROUND_ROBIN, ctx, providers, metrics
        )

    results = await asyncio.gather(*[one_pick() for _ in range(40)])

    # Count distribution per provider.
    counts = {p.provider_name: 0 for p in providers}
    for r in results:
        counts[r.provider_name] += 1
    # Each must be hit exactly 10 times (fairness). itertools.count gives us
    # atomic ticks even when called from coroutines.
    assert counts == {f"p{i}": 10 for i in range(4)}, counts


async def test_round_robin_cursor_advances_monotonically() -> None:
    selector = RoutingStrategySelector()
    providers = [_provider("a"), _provider("b")]
    metrics = {f"{p.provider_name}:m": MagicMock(active_requests=0) for p in providers}
    ctx = RoutingContext(user_input="x", task_type="t", priority="normal")

    picks = [
        selector.select(RoutingStrategy.ROUND_ROBIN, ctx, providers, metrics)
        for _ in range(5)
    ]
    # Strict alternation: a, b, a, b, a
    assert [p.provider_name for p in picks] == ["a", "b", "a", "b", "a"]
