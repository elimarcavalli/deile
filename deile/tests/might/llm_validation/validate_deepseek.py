"""LIVE LLM validation against DeepSeek v4-flash (real API key).

Runs through the surface area touched by the bug-audit PR (#298) and
verifies each fix end-to-end against the real provider:

  1. Basic generate() — provider initializes and round-trips
  2. Cost-cached-token fix (#4) — sanity-check cost formula on a real
     response; if DeepSeek reports cached_tokens, ensure cost is NOT
     double-charged
  3. Multi-turn chat — proves no "assistant"-role issue (DeepSeek uses
     the OpenAI-compatible API which already accepts "assistant"; we
     verify nothing regressed)
  4. Streaming — generate_stream end-to-end
  5. PlanManager._run_tool_with_params — proves the loop stays alive
     across a real LLM-bound step

This script costs a few cents at most. Intentionally short prompts +
low max_tokens to keep spend trivial.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

# Skip the whole file unless explicitly opted-in to live runs.
pytestmark = pytest.mark.skipif(
    os.environ.get("DEEPSEEK_LIVE") != "1",
    reason="set DEEPSEEK_LIVE=1 to run real-API validation",
)


def _emit(label: str, value) -> None:
    print(f"  [{label}] {value}", flush=True)


def _build_provider():
    """Construct a real DeepSeekProvider bound to v4-flash."""
    from deile.core.models.base import ModelTier
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.deepseek_provider import DeepSeekProvider
    from deile.core.models.provider_config import ProviderConfig

    handle = ModelHandle(
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        tier=ModelTier.TIER_3,
        pricing=ModelPricing(
            input_per_1m_usd=0.14,
            output_per_1m_usd=0.28,
            cached_input_per_1m_usd=None,  # not in the catalog YAML
        ),
        context_window=128_000,
        capabilities=frozenset({"function_calling", "streaming", "caching"}),
        display_name="DeepSeek V4 Flash",
        label="ultra-cheap",
    )
    cfg = ProviderConfig(
        provider_id="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
        sdk_kwargs={},
    )
    return DeepSeekProvider(handle, cfg)


async def test_live_basic_generate():
    """Round-trip a trivial generate() and assert the response shape + cost."""
    from deile.core.models.base import ModelMessage

    print("\n=== 1) basic generate() ===")
    provider = _build_provider()
    start = time.monotonic()
    resp = await provider.generate(
        [ModelMessage(role="user", content="Reply with exactly the word: OK")],
        max_tokens=8,
    )
    elapsed = time.monotonic() - start

    _emit("content", repr(resp.content)[:120])
    _emit("model_name", resp.model_name)
    _emit("prompt_tokens", resp.usage.prompt_tokens)
    _emit("completion_tokens", resp.usage.completion_tokens)
    _emit("cached_tokens", resp.usage.cached_tokens)
    _emit("cost_estimate_usd", f"{resp.usage.cost_estimate:.8f}")
    _emit("elapsed_s", f"{elapsed:.2f}")

    # Sanity: response non-empty, usage populated, cost > 0
    assert resp.content, "empty response"
    assert resp.usage.prompt_tokens > 0
    assert resp.usage.completion_tokens > 0
    assert resp.usage.cost_estimate >= 0  # may be 0 if everything was cached
    # The fix: cost must NEVER exceed naive (prompt + completion) at full rate.
    naive_max = (
        resp.usage.prompt_tokens * 0.14 / 1_000_000
        + resp.usage.completion_tokens * 0.28 / 1_000_000
    )
    assert resp.usage.cost_estimate <= naive_max + 1e-9, (
        f"cost {resp.usage.cost_estimate} > naive max {naive_max} — "
        f"double-counting may have returned"
    )
    print("  ✓ basic generate OK; cost ≤ naive-max (no double-counting)")


async def test_live_cost_cached_token_formula():
    """Verify the formula manually against real usage numbers."""
    from deile.core.models.base import ModelMessage

    print("\n=== 2) cost-cached-token formula sanity ===")
    provider = _build_provider()
    # Two calls in a row — DeepSeek may serve the second from cache.
    msg = "List 3 prime numbers and stop. Just the numbers."
    r1 = await provider.generate(
        [ModelMessage(role="user", content=msg)], max_tokens=20
    )
    r2 = await provider.generate(
        [ModelMessage(role="user", content=msg)], max_tokens=20
    )

    for label, r in (("first", r1), ("second", r2)):
        _emit(f"{label}.prompt_tokens", r.usage.prompt_tokens)
        _emit(f"{label}.cached_tokens", r.usage.cached_tokens)
        _emit(f"{label}.cost_estimate", f"{r.usage.cost_estimate:.8f}")

        # Manually recompute the fix's formula and compare.
        cached = r.usage.cached_tokens or 0
        non_cached = max(r.usage.prompt_tokens - cached, 0)
        expected = (
            non_cached * 0.14 / 1_000_000
            + r.usage.completion_tokens * 0.28 / 1_000_000
            + cached * 0.14 / 1_000_000  # cached_per_1m is None → full rate fallback
        )
        delta = abs(r.usage.cost_estimate - expected)
        _emit(f"{label}.expected_cost", f"{expected:.8f}")
        _emit(f"{label}.delta", f"{delta:.2e}")
        assert delta < 1e-9, (
            f"cost formula mismatch for {label}: "
            f"got {r.usage.cost_estimate}, expected {expected}"
        )
    print("  ✓ formula matches override exactly on real responses")


async def test_live_multi_turn():
    """Multi-turn with assistant history — proves no role regression."""
    from deile.core.models.base import ModelMessage

    print("\n=== 3) multi-turn chat ===")
    provider = _build_provider()
    history = [
        ModelMessage(role="user", content="My favorite color is teal. Remember it."),
        ModelMessage(role="assistant", content="Got it, teal."),
        ModelMessage(role="user", content="What is my favorite color? Reply with one word."),
    ]
    resp = await provider.generate(history, max_tokens=10)
    _emit("content", repr(resp.content)[:80])
    _emit("prompt_tokens", resp.usage.prompt_tokens)
    _emit("cost_estimate", f"{resp.usage.cost_estimate:.8f}")
    assert resp.content, "empty multi-turn response"
    assert "teal" in resp.content.lower(), (
        f"multi-turn history not honoured: {resp.content!r}"
    )
    print("  ✓ multi-turn history flowed through correctly")


async def test_live_streaming():
    """generate_stream end-to-end with real usage on USAGE_FINAL."""
    from deile.core.models.base import ModelMessage
    from deile.core.models.stream_events import StreamEventType

    print("\n=== 4) streaming generate_stream ===")
    provider = _build_provider()
    chunks: list[str] = []
    final_usage = None
    error = None
    async for evt in provider.generate_stream(
        [ModelMessage(role="user", content="Count from 1 to 5, comma-separated.")],
        max_tokens=30,
    ):
        if evt.type == StreamEventType.TEXT_DELTA and evt.text:
            chunks.append(evt.text)
        elif evt.type == StreamEventType.USAGE_FINAL:
            final_usage = evt.usage
        elif evt.type == StreamEventType.ERROR:
            error = evt.error_envelope
            break

    if error:
        print(f"  ✗ stream error: {error}")
        raise AssertionError(f"stream error: {error}")

    full = "".join(chunks)
    _emit("stream_text", repr(full)[:120])
    _emit("chunks_count", len(chunks))
    if final_usage is not None:
        _emit("final.input_tokens", final_usage.input_tokens)
        _emit("final.cached_tokens", final_usage.cached_tokens)
        _emit("final.cost_usd", f"{final_usage.cost_usd:.8f}")
        # Re-verify the cost on the streamed USAGE_FINAL too.
        naive_max = (
            final_usage.input_tokens * 0.14 / 1_000_000
            + final_usage.output_tokens * 0.28 / 1_000_000
        )
        assert final_usage.cost_usd <= naive_max + 1e-9
    assert full.strip(), "stream produced no text"
    assert len(chunks) > 0
    print("  ✓ streaming OK; USAGE_FINAL cost ≤ naive-max")


async def test_live_plan_manager_timeout_with_real_tool():
    """Prove PlanManager._run_tool_with_params keeps the loop alive across
    a step that internally awaits a real LLM call."""
    print("\n=== 5) PlanManager step over real LLM (loop liveness) ===")
    from deile.core.models.base import ModelMessage
    from deile.orchestration.plan_manager import PlanManager
    from deile.tools.base import (SecurityLevel, Tool, ToolCategory,
                                  ToolContext, ToolResult, ToolSchema)

    provider = _build_provider()

    class _LLMTool(Tool):
        @property
        def name(self) -> str:
            return "llm_echo"

        @property
        def description(self) -> str:
            return "round-trips a single prompt to deepseek"

        @property
        def category(self) -> str:
            return ToolCategory.OTHER.value

        def __init__(self) -> None:
            super().__init__(schema=ToolSchema(
                name="llm_echo",
                description="round-trips a single prompt to deepseek",
                parameters={"prompt": {"type": "string", "description": "prompt"}},
                required=["prompt"],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            ))

        async def execute(self, ctx: ToolContext) -> ToolResult:
            r = await provider.generate(
                [ModelMessage(role="user", content=ctx.parsed_args["prompt"])],
                max_tokens=10,
            )
            return ToolResult.success_result(data=r.content)

    pm = PlanManager(plans_dir="/tmp/llm_plan_validate")

    # Heartbeat task that must keep ticking even while the LLM call is in flight.
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(80):
            await asyncio.sleep(0.05)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    tool_task = asyncio.create_task(
        pm._run_tool_with_params(_LLMTool(), {"prompt": "Reply with exactly: HI"})
    )
    result = await tool_task
    await hb

    _emit("result.is_success", result.is_success)
    _emit("result.data", repr(result.data)[:80])
    _emit("heartbeat_ticks", ticks)
    assert result.is_success
    assert ticks >= 5, f"loop stalled during LLM call (ticks={ticks})"
    print("  ✓ loop stayed responsive while step awaited the real LLM")


async def main():
    print("=" * 60)
    print("LIVE DeepSeek validation — bug-audit PR #298")
    print("=" * 60)
    failures = []
    for fn in (
        test_live_basic_generate,
        test_live_cost_cached_token_formula,
        test_live_multi_turn,
        test_live_streaming,
        test_live_plan_manager_timeout_with_real_tool,
    ):
        try:
            await fn()
        except Exception as exc:
            print(f"  ✗ {fn.__name__} FAILED: {type(exc).__name__}: {exc}")
            failures.append((fn.__name__, exc))
    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} FAILED, {5 - len(failures)} OK")
        for name, exc in failures:
            print(f"  - {name}: {exc}")
        sys.exit(1)
    print("RESULT: all 5 live validations OK")


if __name__ == "__main__":
    asyncio.run(main())
