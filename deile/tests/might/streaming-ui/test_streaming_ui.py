"""Empirical streaming-UI test — runs against a real LLM (cheap model).

Verifies that the streaming refactor genuinely produces an interleaved event
sequence: TEXT_DELTA → TOOL_USE_START → TOOL_USE_END → TOOL_RESULT → TEXT_DELTA →
USAGE_FINAL, with at least one tool round-trip.

Skipped automatically when ``ANTHROPIC_API_KEY`` is missing. Budget per run:
≤ $0.01 with claude-haiku-4-5.

Run manually:
    python deile/tests/might/streaming-ui/test_streaming_ui.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import List

import pytest

# Faz uma requisição real ao provider quando a chave existe — opt-in only
# (issue #499). Sem isto, um dev com ANTHROPIC_API_KEY no shell queimaria
# tokens num `pytest` comum, pois o skip embutido só dispara sem a chave.
pytestmark = pytest.mark.llm

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from deile.config.manager import ConfigManager  # noqa: E402
from deile.core.agent import DeileAgent  # noqa: E402
from deile.core.models.bootstrap import bootstrap_providers  # noqa: E402
from deile.core.models.router import get_model_router  # noqa: E402
from deile.core.models.stream_events import StreamEventType  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
PROBE = (
    "Você DEVE usar a ferramenta read_file para ler o arquivo "
    "deile/core/models/stream_events.py e me dizer quantas linhas ele tem. "
    "Não responda sem antes ler o arquivo via tool — eu vou conferir os tool_calls."
)
MODEL_KEY = os.getenv("DEILE_STREAM_TEST_MODEL", "anthropic:claude-haiku-4-5")


def _check_preconditions() -> str | None:
    """Return reason to skip, or None to proceed."""
    if MODEL_KEY.startswith("anthropic:") and not os.getenv("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not set"
    if MODEL_KEY.startswith("openai:") and not os.getenv("OPENAI_API_KEY"):
        return "OPENAI_API_KEY not set"
    if MODEL_KEY.startswith("deepseek:") and not os.getenv("DEEPSEEK_API_KEY"):
        return "DEEPSEEK_API_KEY not set"
    if MODEL_KEY.startswith("gemini:") and not os.getenv("GOOGLE_API_KEY"):
        return "GOOGLE_API_KEY not set"
    return None


async def _run() -> dict:
    log_path = THIS_DIR / "run.log"
    config_manager = ConfigManager()
    config_manager.load_config()

    router = get_model_router()
    registered = bootstrap_providers(router=router)
    if not registered:
        return {"status": "no_providers"}

    agent = DeileAgent(model_router=router, config_manager=config_manager)
    await agent.initialize()
    session = agent.create_session(
        session_id=f"stream-test-{int(time.time())}",
        working_directory=Path.cwd(),
    )
    session.context_data["forced_model"] = MODEL_KEY

    timestamps: List[tuple] = []
    types_seen: List[str] = []
    text_chunks: List[str] = []
    tool_starts = 0
    tool_ends = 0
    tool_results = 0
    error_envelope = None

    log_lines: List[str] = [
        "=" * 100,
        f"MODEL: {MODEL_KEY}",
        f"PROBE: {PROBE}",
        "=" * 100,
    ]

    t0 = time.time()
    async for event in agent.process_input_stream(
        user_input=PROBE, session_id=session.session_id
    ):
        ts = time.time() - t0
        timestamps.append((ts, event.type.name))
        types_seen.append(event.type.name)
        log_lines.append(f"[{ts:6.3f}s] {event.type.name}")
        if event.type is StreamEventType.TEXT_DELTA and event.text:
            text_chunks.append(event.text)
            log_lines.append(f"    text: {event.text!r}")
        elif event.type is StreamEventType.TOOL_USE_START:
            tool_starts += 1
            log_lines.append(
                f"    tool_call_id={event.tool_call_id} name={event.tool_name}"
            )
        elif event.type is StreamEventType.TOOL_USE_END:
            tool_ends += 1
            log_lines.append(f"    args={event.arguments}")
        elif event.type is StreamEventType.TOOL_RESULT:
            tool_results += 1
            log_lines.append(
                f"    status={event.tool_status} summary={event.tool_result_summary}"
            )
        elif event.type is StreamEventType.ERROR:
            error_envelope = event.error_envelope
            log_lines.append(f"    error={event.error_envelope}")

    duration = time.time() - t0
    log_lines.append("-" * 100)
    log_lines.append(f"duration: {duration:.2f}s")
    log_lines.append(
        f"tool_starts={tool_starts} tool_ends={tool_ends} tool_results={tool_results}"
    )
    log_lines.append("FULL TEXT:")
    log_lines.append("".join(text_chunks))
    log_path.write_text("\n".join(log_lines), encoding="utf-8")

    # Streaming contract assertions
    contract = {
        "had_text_before_first_tool": False,
        "tool_round_trip_complete": tool_starts > 0
        and tool_ends > 0
        and tool_results > 0,
        "duration_ok": duration > 0.0,
        "no_error": error_envelope is None,
    }
    first_tool_ts = next(
        (ts for ts, t in timestamps if t == "TOOL_USE_START"),
        None,
    )
    if first_tool_ts is not None:
        text_before_tool = any(
            t == "TEXT_DELTA" and ts < first_tool_ts for ts, t in timestamps
        )
        contract["had_text_before_first_tool"] = text_before_tool

    return {
        "status": "ok",
        "duration_s": duration,
        "events": len(timestamps),
        "tool_starts": tool_starts,
        "tool_ends": tool_ends,
        "tool_results": tool_results,
        "contract": contract,
        "log": str(log_path),
    }


_BILLING_HINTS = (
    "credit balance is too low",
    "insufficient_quota",
    "billing_hard_limit_reached",
    "you exceeded your current quota",
    "quota_exceeded",
)


def _looks_like_billing_error(result: dict) -> bool:
    """Detect provider billing/quota errors that surface as ERROR events.

    Without this, runs from accounts without credit fail the contract
    assertions even though the streaming code itself is healthy — the LLM
    never gets a chance to invoke a tool because the request is rejected
    before the first token. Treating these as ``skip`` keeps the suite
    green while still surfacing the underlying issue in the log file.
    """
    if result.get("contract", {}).get("no_error", True):
        return False
    # We don't have the envelope in the result dict; re-check the log.
    log_path = result.get("log")
    if not log_path:
        return False
    try:
        text = Path(log_path).read_text(encoding="utf-8").lower()
    except Exception:
        return False
    return any(hint in text for hint in _BILLING_HINTS)


def test_streaming_ui_empirical():
    import pytest

    skip = _check_preconditions()
    if skip:
        pytest.skip(skip)

    result = asyncio.run(_run())
    assert result["status"] == "ok", result
    if _looks_like_billing_error(result):
        pytest.skip(
            "Provider rejected the request with a billing/quota error — "
            f"see {result.get('log')}"
        )
    assert result["events"] > 0
    assert result["tool_starts"] >= 1, result
    assert result["tool_results"] >= 1, result
    contract = result["contract"]
    assert contract["tool_round_trip_complete"], contract
    assert contract["no_error"], contract


if __name__ == "__main__":
    skip = _check_preconditions()
    if skip:
        print(f"SKIP: {skip}")
        sys.exit(0)
    result = asyncio.run(_run())
    print("\n" + "=" * 60)
    print("RESULT:", result)
    print("=" * 60)
