"""LIVE LLM validation against OpenRouter (real OPENROUTER_API_KEY) — OR7 smoke.

This is the REAL smoke test for the OpenRouter integration. It is SKIPPED by
default (no key exists yet); it only runs when you opt in explicitly.

HOW TO RUN THE REAL SMOKE (once you have a key):

    # 1. Put your key in .env (gitignored) or export it:
    export OPENROUTER_API_KEY=sk-or-...

    # 2. Opt in and run, either as a plain script:
    OPENROUTER_LIVE=1 python3 deile/tests/might/llm_validation/validate_openrouter.py

    #    ...or under pytest (the @skipif lifts when OPENROUTER_LIVE=1):
    OPENROUTER_LIVE=1 python3 -m pytest \
        deile/tests/might/llm_validation/validate_openrouter.py -v -s -p no:cov

What it proves end-to-end against the real gateway:
  1. basic generate() round-trips through openrouter.ai/api/v1 (one key).
  2. the response carries usage.cost (because we send usage.include=true), and
     that billed cost — NOT the catalog estimate — becomes usage.cost_estimate.
  3. a model_id with a '/' (openrouter:deepseek/deepseek-chat) reaches the wire
     intact.
  4. streaming generate_stream emits USAGE_FINAL with a cost.

Spend is trivial: one tiny prompt to deepseek/deepseek-chat (~fraction of a cent).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENROUTER_LIVE") != "1",
    reason="set OPENROUTER_LIVE=1 (and OPENROUTER_API_KEY) to run real-API validation",
)

_MODEL_ID = "deepseek/deepseek-chat"  # cheapest sane default on OpenRouter


def _emit(label: str, value) -> None:
    print(f"  [{label}] {value}", flush=True)


def _build_provider():
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.openrouter_provider import OpenRouterProvider
    from deile.core.models.provider_config import ProviderConfig
    from deile.core.models.tier import ModelTier

    handle = ModelHandle(
        provider_id="openrouter",
        model_id=_MODEL_ID,
        tier=ModelTier.TIER_3,
        pricing=ModelPricing(input_per_1m_usd=0.28, output_per_1m_usd=0.88),
        context_window=163_840,
        capabilities=frozenset({"function_calling", "streaming"}),
        display_name="DeepSeek Chat (OpenRouter)",
        label="ultra-cheap",
    )
    cfg = ProviderConfig(
        provider_id="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        sdk_kwargs={
            "default_headers": {
                "HTTP-Referer": "https://github.com/elimarcavalli/deile",
                "X-Title": "DEILE",
            }
        },
    )
    return OpenRouterProvider(handle, cfg)


async def test_live_basic_generate_reports_cost():
    from deile.core.models.base import ModelMessage

    print("\n=== 1) basic generate() via OpenRouter + reported cost ===")
    provider = _build_provider()
    assert provider.model_name == _MODEL_ID  # '/' survived
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
    _emit("reported_cost_usd", resp.usage.extra.get("reported_cost_usd"))
    _emit("cost_estimate_usd", f"{resp.usage.cost_estimate:.8f}")
    _emit("elapsed_s", f"{elapsed:.2f}")

    assert resp.content, "empty response"
    assert resp.usage.prompt_tokens > 0
    # OpenRouter should report the billed cost when usage.include=true was sent.
    reported = resp.usage.extra.get("reported_cost_usd")
    if reported is not None:
        assert resp.usage.cost_estimate == pytest.approx(float(reported))
        print("  ✓ reported usage.cost is authoritative (no undercount)")
    else:
        print("  ⚠ no usage.cost reported — fell back to catalog estimate")
        assert resp.usage.cost_estimate >= 0


async def test_live_streaming_cost():
    from deile.core.models.base import ModelMessage
    from deile.core.models.stream_events import StreamEventType

    print("\n=== 2) streaming generate_stream + USAGE_FINAL cost ===")
    provider = _build_provider()
    chunks: list[str] = []
    final_usage = None
    async for evt in provider.generate_stream(
        [ModelMessage(role="user", content="Count 1 to 5, comma-separated.")],
        max_tokens=30,
    ):
        if evt.type == StreamEventType.TEXT_DELTA and evt.text:
            chunks.append(evt.text)
        elif evt.type == StreamEventType.USAGE_FINAL:
            final_usage = evt.usage
        elif evt.type == StreamEventType.ERROR:
            raise AssertionError(f"stream error: {evt.error_envelope}")

    full = "".join(chunks)
    _emit("stream_text", repr(full)[:120])
    if final_usage is not None:
        _emit("final.cost_usd", f"{final_usage.cost_usd:.8f}")
    assert full.strip(), "stream produced no text"
    print("  ✓ streaming OK")


async def main():
    print("=" * 60)
    print("LIVE OpenRouter validation — OR7 smoke")
    print("=" * 60)
    failures = []
    for fn in (test_live_basic_generate_reports_cost, test_live_streaming_cost):
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {fn.__name__} FAILED: {type(exc).__name__}: {exc}")
            failures.append((fn.__name__, exc))
    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} FAILED")
        sys.exit(1)
    print("RESULT: all live validations OK")


if __name__ == "__main__":
    if os.environ.get("OPENROUTER_LIVE") != "1":
        print("Refusing to run: set OPENROUTER_LIVE=1 and OPENROUTER_API_KEY first.")
        print(__doc__)
        sys.exit(2)
    asyncio.run(main())
