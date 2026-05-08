"""Tests for deile.cron.agent_bridge (intent #86)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.cron.agent_bridge import make_fire_callback
from deile.cron.store import CronEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(entry_id: str = "cron-abc123", prompt: str = "do something") -> CronEntry:
    """Return a minimal one-shot CronEntry for testing."""
    return CronEntry(
        id=entry_id,
        prompt=prompt,
        run_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def _make_agent_response(content: str) -> MagicMock:
    """Return a mock AgentResponse-like object."""
    response = MagicMock()
    response.content = content
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_happy_path_returns_summary():
    """Callback returns the response content for a normal successful call."""
    expected = "Task completed successfully"
    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=_make_agent_response(expected))

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    result = await cb(_make_entry())

    assert result == expected
    agent.process_input.assert_awaited_once()


@pytest.mark.unit
async def test_long_response_truncated_to_default():
    """Response longer than max_summary_chars (500) is truncated with ellipsis."""
    long_text = "x" * 600
    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=_make_agent_response(long_text))

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    result = await cb(_make_entry())

    assert len(result) == 500
    assert result.endswith("…")
    assert result[:499] == "x" * 499


@pytest.mark.unit
async def test_custom_max_summary_chars_respected():
    """Custom max_summary_chars is honoured."""
    text = "hello world — this is a longer reply"
    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=_make_agent_response(text))

    async def provider():
        return agent

    cb = make_fire_callback(provider, max_summary_chars=10)
    result = await cb(_make_entry())

    assert len(result) == 10
    assert result.endswith("…")


@pytest.mark.unit
async def test_process_input_raises_returns_error_string():
    """When agent.process_input raises, callback returns error string, not raises."""
    agent = AsyncMock()
    agent.process_input = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    result = await cb(_make_entry())

    assert result.startswith("error: RuntimeError:")
    assert "LLM timeout" in result


@pytest.mark.unit
async def test_agent_provider_raises_returns_error_string():
    """When agent_provider itself raises, callback returns error string, not raises."""
    async def failing_provider():
        raise ConnectionError("DB unreachable")

    cb = make_fire_callback(failing_provider)
    result = await cb(_make_entry())

    assert result.startswith("error: ConnectionError:")
    assert "DB unreachable" in result


@pytest.mark.unit
async def test_session_id_includes_entry_id():
    """process_input is called with session_id that contains entry.id."""
    entry = _make_entry(entry_id="cron-testentry42")
    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=_make_agent_response("ok"))

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    await cb(entry)

    call_kwargs = agent.process_input.call_args
    assert call_kwargs.kwargs.get("session_id") == "cron-cron-testentry42"


@pytest.mark.unit
async def test_response_without_content_attribute_uses_str():
    """Response with no .content attribute falls back to str(response)."""

    class NoContentResponse:
        def __str__(self) -> str:
            return "fallback text"

    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=NoContentResponse())

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    result = await cb(_make_entry())

    # Should have used str() and not raised
    assert isinstance(result, str)
    assert "fallback text" in result


@pytest.mark.unit
async def test_short_response_not_truncated():
    """Response exactly at max_summary_chars is not truncated."""
    text = "a" * 500
    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=_make_agent_response(text))

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    result = await cb(_make_entry())

    assert result == text
    assert not result.endswith("…")


@pytest.mark.unit
async def test_prompt_forwarded_to_process_input():
    """entry.prompt is forwarded verbatim as the first positional arg."""
    prompt = "summarize yesterday's commits"
    entry = _make_entry(prompt=prompt)
    agent = AsyncMock()
    agent.process_input = AsyncMock(return_value=_make_agent_response("done"))

    async def provider():
        return agent

    cb = make_fire_callback(provider)
    await cb(entry)

    call_args = agent.process_input.call_args
    assert call_args.args[0] == prompt
