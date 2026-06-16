"""Regression tests for OpenAI/DeepSeek cost calculation with cached tokens.

Bug: OpenAI/DeepSeek return ``prompt_tokens`` as the FULL input total —
the cached subset (``prompt_tokens_details.cached_tokens`` for OpenAI,
``prompt_cache_hit_tokens`` for DeepSeek) is INCLUDED in ``prompt_tokens``.
The base ``estimate_cost`` (designed for Anthropic, whose ``input_tokens``
excludes the cached portion) charged ``prompt_tokens`` at the full input
rate AND ``cached_tokens`` again at the cached rate — double-counting the
cached portion and inflating session/daily/monthly cost reports, which in
turn trips ``BudgetGuard`` earlier than the real spend warrants.

Fix: OpenAIProvider overrides ``estimate_cost`` so ``prompt_tokens`` is
treated as a superset; only ``prompt_tokens - cached_tokens`` is charged
at the full rate.
"""

from __future__ import annotations

from types import SimpleNamespace

from deile.core.models.base import ModelUsage
from deile.core.models.deepseek_provider import DeepSeekProvider
from deile.core.models.openai_provider import OpenAIProvider


class _FakeHandle:
    def __init__(self, pricing) -> None:
        self.pricing = pricing


def _make_openai_provider(
    input_per_1m: float, cached_per_1m: float | None
) -> OpenAIProvider:
    """Build an OpenAIProvider with stubbed pricing — no client init needed."""
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._handle = _FakeHandle(
        SimpleNamespace(
            input_per_1m_usd=input_per_1m,
            output_per_1m_usd=10.0,
            cached_input_per_1m_usd=cached_per_1m,
        )
    )
    return provider


def _make_deepseek_provider() -> DeepSeekProvider:
    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    provider._handle = _FakeHandle(
        SimpleNamespace(
            input_per_1m_usd=1.0,
            output_per_1m_usd=2.0,
            cached_input_per_1m_usd=0.1,
        )
    )
    return provider


def test_openai_cached_tokens_not_double_charged() -> None:
    """1M total prompt, 700k cached, 300k non-cached:
    cost = 300k * $5 + 700k * $1 + 0 output = $1.50 + $0.70 = $2.20.
    The base impl would have charged 1M * $5 + 700k * $1 = $5.70.
    """
    provider = _make_openai_provider(input_per_1m=5.0, cached_per_1m=1.0)
    usage = ModelUsage(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=700_000,
    )
    cost = provider.estimate_cost(usage)
    assert abs(cost - 2.20) < 1e-6, cost


def test_openai_no_cache_matches_full_input_rate() -> None:
    provider = _make_openai_provider(input_per_1m=5.0, cached_per_1m=1.0)
    usage = ModelUsage(
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
        total_tokens=1_500_000,
        cached_tokens=0,
    )
    # 1M * $5 input + 0.5M * $10 output = $5 + $5 = $10
    cost = provider.estimate_cost(usage)
    assert abs(cost - 10.0) < 1e-6, cost


def test_openai_cached_without_cached_price_falls_back_to_full_rate() -> None:
    """When the catalog has no cached_input_per_1m_usd, the cached portion is
    billed at the standard input rate (i.e. no implicit discount)."""
    provider = _make_openai_provider(input_per_1m=5.0, cached_per_1m=None)
    usage = ModelUsage(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=400_000,
    )
    # 600k non-cached @ $5 + 400k cached @ $5 = $5
    cost = provider.estimate_cost(usage)
    assert abs(cost - 5.0) < 1e-6, cost


def test_openai_cached_exceeding_prompt_does_not_go_negative() -> None:
    """Defensive: usage data with cached > prompt (impossible per API contract
    but might arise from upstream bugs) must clamp to zero, not negative."""
    provider = _make_openai_provider(input_per_1m=5.0, cached_per_1m=1.0)
    usage = ModelUsage(
        prompt_tokens=100_000,
        completion_tokens=0,
        total_tokens=100_000,
        cached_tokens=500_000,
    )
    cost = provider.estimate_cost(usage)
    # No negative input cost; only cached_cost = 500k * $1 = $0.50.
    assert cost >= 0.0
    assert abs(cost - 0.50) < 1e-6, cost


def test_deepseek_inherits_openai_estimate_cost() -> None:
    provider = _make_deepseek_provider()
    usage = ModelUsage(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        cached_tokens=200_000,
    )
    # 800k non-cached @ $1 + 200k cached @ $0.10 = $0.80 + $0.02 = $0.82
    cost = provider.estimate_cost(usage)
    assert abs(cost - 0.82) < 1e-6, cost


def test_estimate_cost_with_no_pricing_returns_zero() -> None:
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._handle = None  # no catalog pricing
    usage = ModelUsage(prompt_tokens=1_000_000, cached_tokens=100_000)
    assert provider.estimate_cost(usage) == 0.0
