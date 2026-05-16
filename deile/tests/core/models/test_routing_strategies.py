"""Tests: RoutingStrategySelector — legacy strategy-based provider selection.

Covers every :class:`RoutingStrategy` branch of
:meth:`RoutingStrategySelector.select`, the unknown-strategy fallback and the
empty-provider-list guard. Uses lightweight fake providers (no SDK, no I/O).
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from deile.core.models.base import (ModelMessage, ModelProvider, ModelResponse,
                                    ModelSize, ModelType)
from deile.core.models.routing_strategies import (ModelMetrics, RoutingContext,
                                                  RoutingStrategy,
                                                  RoutingStrategySelector,
                                                  _provider_key)
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent

# ---------------------------------------------------------------------------
# Lightweight fake provider — no SDK, no network.
# ---------------------------------------------------------------------------


class _FakeProvider(ModelProvider):
    """Minimal concrete ModelProvider for routing tests."""

    def __init__(self, provider_name: str, model_name: str,
                 model_size: ModelSize = ModelSize.MEDIUM):
        super().__init__(model_name=model_name)
        self._provider_name = provider_name
        self._model_size = model_size

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT]

    @property
    def model_size(self) -> ModelSize:
        return self._model_size

    async def generate(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> ModelResponse:
        return ModelResponse(content="ok", model_name=self.model_name)

    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List] = None,
        **kwargs,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        yield UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="ok")


def _metrics_for(providers: List[ModelProvider]) -> dict:
    """Build a fresh ModelMetrics map keyed exactly like the selector expects."""
    return {_provider_key(p): ModelMetrics() for p in providers}


def _ctx(user_input: str = "", estimated_tokens: int = 0) -> RoutingContext:
    return RoutingContext(user_input=user_input, estimated_tokens=estimated_tokens)


# ---------------------------------------------------------------------------
# ROUND_ROBIN
# ---------------------------------------------------------------------------


class TestRoundRobin:
    def test_cursor_advances_and_cycles(self):
        selector = RoutingStrategySelector()
        providers = [
            _FakeProvider("anthropic", "claude"),
            _FakeProvider("openai", "gpt"),
            _FakeProvider("gemini", "flash"),
        ]
        metrics = _metrics_for(providers)
        ctx = _ctx()

        picks = [
            selector.select(RoutingStrategy.ROUND_ROBIN, ctx, providers, metrics)
            for _ in range(6)
        ]

        # First pass cycles through all providers in order.
        assert picks[0:3] == providers
        # Second pass repeats the cycle (cursor wrapped).
        assert picks[3:6] == providers
        assert selector._round_robin_index == 6

    def test_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select(
            RoutingStrategy.ROUND_ROBIN, _ctx(), [], {}
        )
        assert result is None


# ---------------------------------------------------------------------------
# LEAST_BUSY
# ---------------------------------------------------------------------------


class TestLeastBusy:
    def test_selects_provider_with_fewest_active_requests(self):
        selector = RoutingStrategySelector()
        busy = _FakeProvider("anthropic", "claude")
        idle = _FakeProvider("openai", "gpt")
        providers = [busy, idle]
        metrics = _metrics_for(providers)
        metrics[_provider_key(busy)].active_requests = 5
        metrics[_provider_key(idle)].active_requests = 1

        result = selector.select(
            RoutingStrategy.LEAST_BUSY, _ctx(), providers, metrics
        )
        assert result is idle

    def test_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select(RoutingStrategy.LEAST_BUSY, _ctx(), [], {})
        assert result is None


# ---------------------------------------------------------------------------
# TASK_OPTIMIZED
# ---------------------------------------------------------------------------


class TestTaskOptimized:
    def test_selects_by_preferred_size_then_best_success_rate(self):
        selector = RoutingStrategySelector()
        # "create" maps to code_generation -> ModelSize.LARGE.
        large_low = _FakeProvider("a", "m1", ModelSize.LARGE)
        large_high = _FakeProvider("b", "m2", ModelSize.LARGE)
        small = _FakeProvider("c", "m3", ModelSize.SMALL)
        providers = [large_low, large_high, small]
        metrics = _metrics_for(providers)
        metrics[_provider_key(large_low)].success_rate = 0.7
        metrics[_provider_key(large_high)].success_rate = 0.95

        result = selector.select(
            RoutingStrategy.TASK_OPTIMIZED,
            _ctx(user_input="create a new module"),
            providers,
            metrics,
        )
        # Filtered to LARGE, then the higher success_rate wins.
        assert result is large_high

    def test_falls_back_to_best_overall_when_no_size_match(self):
        selector = RoutingStrategySelector()
        # "create" wants LARGE but no LARGE provider exists -> overall best.
        weak = _FakeProvider("a", "m1", ModelSize.SMALL)
        strong = _FakeProvider("b", "m2", ModelSize.MEDIUM)
        providers = [weak, strong]
        metrics = _metrics_for(providers)
        metrics[_provider_key(weak)].success_rate = 0.5
        metrics[_provider_key(strong)].success_rate = 0.99

        result = selector.select(
            RoutingStrategy.TASK_OPTIMIZED,
            _ctx(user_input="create something"),
            providers,
            metrics,
        )
        assert result is strong

    def test_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select(RoutingStrategy.TASK_OPTIMIZED, _ctx(), [], {})
        assert result is None


# ---------------------------------------------------------------------------
# COST_OPTIMIZED
# ---------------------------------------------------------------------------


class TestCostOptimized:
    def test_selects_cheapest_estimated_cost(self):
        selector = RoutingStrategySelector()
        cheap = _FakeProvider("a", "m1")
        pricey = _FakeProvider("b", "m2")
        providers = [pricey, cheap]
        metrics = _metrics_for(providers)
        metrics[_provider_key(cheap)].cost_per_token = 0.001
        metrics[_provider_key(pricey)].cost_per_token = 0.05

        result = selector.select(
            RoutingStrategy.COST_OPTIMIZED,
            _ctx(estimated_tokens=1000),
            providers,
            metrics,
        )
        assert result is cheap

    def test_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select(RoutingStrategy.COST_OPTIMIZED, _ctx(), [], {})
        assert result is None


# ---------------------------------------------------------------------------
# PERFORMANCE_OPTIMIZED
# ---------------------------------------------------------------------------


class TestPerformanceOptimized:
    def test_selects_best_performance_score(self):
        selector = RoutingStrategySelector()
        slow = _FakeProvider("a", "m1")
        fast = _FakeProvider("b", "m2")
        providers = [slow, fast]
        metrics = _metrics_for(providers)
        # score = success_rate / avg_response_time (higher = better)
        metrics[_provider_key(slow)].success_rate = 0.9
        metrics[_provider_key(slow)].avg_response_time = 3.0
        metrics[_provider_key(fast)].success_rate = 0.9
        metrics[_provider_key(fast)].avg_response_time = 0.5

        result = selector.select(
            RoutingStrategy.PERFORMANCE_OPTIMIZED, _ctx(), providers, metrics
        )
        assert result is fast

    def test_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select(
            RoutingStrategy.PERFORMANCE_OPTIMIZED, _ctx(), [], {}
        )
        assert result is None


# ---------------------------------------------------------------------------
# LOAD_BALANCED
# ---------------------------------------------------------------------------


class TestLoadBalanced:
    def test_selects_lowest_load_score(self):
        selector = RoutingStrategySelector()
        heavy = _FakeProvider("a", "m1")
        light = _FakeProvider("b", "m2")
        providers = [heavy, light]
        metrics = _metrics_for(providers)
        # load_score = (active+1) * response_time / max(success_rate, 0.1)
        metrics[_provider_key(heavy)].active_requests = 8
        metrics[_provider_key(heavy)].avg_response_time = 2.0
        metrics[_provider_key(heavy)].success_rate = 0.9
        metrics[_provider_key(light)].active_requests = 0
        metrics[_provider_key(light)].avg_response_time = 0.5
        metrics[_provider_key(light)].success_rate = 0.9

        result = selector.select(
            RoutingStrategy.LOAD_BALANCED, _ctx(), providers, metrics
        )
        assert result is light

    def test_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select(RoutingStrategy.LOAD_BALANCED, _ctx(), [], {})
        assert result is None


# ---------------------------------------------------------------------------
# Unknown strategy branch
# ---------------------------------------------------------------------------


class TestUnknownStrategy:
    def test_unknown_strategy_returns_first_provider(self):
        selector = RoutingStrategySelector()
        providers = [
            _FakeProvider("a", "m1"),
            _FakeProvider("b", "m2"),
        ]
        metrics = _metrics_for(providers)

        # An object that is not a recognized RoutingStrategy member.
        result = selector.select(
            "not_a_strategy", _ctx(), providers, metrics
        )
        assert result is providers[0]

    def test_unknown_strategy_empty_providers_returns_none(self):
        selector = RoutingStrategySelector()
        result = selector.select("not_a_strategy", _ctx(), [], {})
        assert result is None
