"""Tests: GeminiProvider unified contract — Phase 6."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.core.models.base import ModelMessage, ModelSize, ModelType
from deile.core.models.gemini_provider import GeminiProvider
from deile.core.models.stream_events import StreamEventType
from deile.core.models.tier import ModelTier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider() -> GeminiProvider:
    """Bare GeminiProvider instance bypassing __init__ side-effects (mirrors existing tests).

    Only the methods under test are exercised; no real SDK client is created.
    """
    p = GeminiProvider.__new__(GeminiProvider)
    # Minimal attributes needed by the methods under test
    p.model_name = "gemini-1.5-pro"
    p._model_size_mapping = {
        "gemini-1.5-pro": ModelSize.LARGE,
        "gemini-1.5-flash": ModelSize.MEDIUM,
    }
    p._chat_sessions = {}
    p._request_count = 0
    p._total_tokens = 0
    p._is_available = True
    p.generation_config = {"temperature": 0.1, "max_output_tokens": 8192}
    p.config = {}
    return p


# ---------------------------------------------------------------------------
# Identity — provider_id and tier inherit from base correctly
# ---------------------------------------------------------------------------

def test_provider_name(provider):
    assert provider.provider_name == "gemini"


def test_provider_id_matches_provider_name(provider):
    assert provider.provider_id == "gemini"


def test_supported_types(provider):
    types_ = provider.supported_types
    assert ModelType.CHAT in types_
    assert ModelType.VISION in types_


def test_model_size_for_pro(provider):
    assert provider.model_size == ModelSize.LARGE


def test_tier_derived_from_model_size(provider):
    # LARGE → TIER_1
    assert provider.tier == ModelTier.TIER_1


def test_pricing_is_none(provider):
    assert provider.pricing is None


# ---------------------------------------------------------------------------
# generate_stream() — yields UnifiedStreamEvent, not raw strings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_stream_yields_unified_events(provider):
    mock_response = MagicMock()
    mock_response.content = "Hello World test"
    mock_response.usage = MagicMock(prompt_tokens=5, completion_tokens=3, cached_tokens=0, cost_estimate=0.0)
    mock_response.usage.prompt_tokens = 5
    mock_response.usage.completion_tokens = 3
    mock_response.usage.cached_tokens = 0
    mock_response.usage.cost_estimate = 0.0

    provider.generate = AsyncMock(return_value=mock_response)

    events = []
    async for ev in provider.generate_stream([ModelMessage(role="user", content="hi")]):
        events.append(ev)

    from deile.core.models.stream_events import UnifiedStreamEvent
    assert all(isinstance(ev, UnifiedStreamEvent) for ev in events)

    text_events = [e for e in events if e.type == StreamEventType.TEXT_DELTA]
    usage_events = [e for e in events if e.type == StreamEventType.USAGE_FINAL]

    assert len(text_events) >= 1
    full_text = "".join(e.text for e in text_events if e.text)
    assert "Hello" in full_text
    assert len(usage_events) == 1
    assert usage_events[0].usage.input_tokens == 5


# ---------------------------------------------------------------------------
# chat_with_tools() — unified signature -> (text, results, usage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unified_chat_with_tools_returns_triple(provider):
    """Unified chat_with_tools must return (str, list, ModelUsage)."""
    provider.create_chat_session = AsyncMock(return_value=MagicMock())
    provider._gemini_chat_with_tools = AsyncMock(return_value=("answer", [], __import__('deile.core.models.base', fromlist=['ModelUsage']).ModelUsage()))

    text, tool_results, usage = await provider.chat_with_tools(
        messages=[ModelMessage(role="user", content="hello")],
        tools=[],
        system_instruction="Be helpful",
    )

    assert isinstance(text, str)
    assert isinstance(tool_results, list)
    from deile.core.models.base import ModelUsage
    assert isinstance(usage, ModelUsage)


@pytest.mark.asyncio
async def test_unified_chat_with_tools_text_passes_through(provider):
    provider.create_chat_session = AsyncMock(return_value=MagicMock())
    provider._gemini_chat_with_tools = AsyncMock(return_value=("Paris is the capital", [], __import__('deile.core.models.base', fromlist=['ModelUsage']).ModelUsage()))

    text, _, _ = await provider.chat_with_tools(
        messages=[ModelMessage(role="user", content="Capital of France?")],
        tools=[],
    )
    assert text == "Paris is the capital"


# ---------------------------------------------------------------------------
# _messages_to_gemini_user_input() helper
# ---------------------------------------------------------------------------

def test_messages_to_gemini_user_input_last_user(provider):
    messages = [
        ModelMessage(role="system", content="sys"),
        ModelMessage(role="user", content="first"),
        ModelMessage(role="assistant", content="reply"),
        ModelMessage(role="user", content="last question"),
    ]
    result = provider._messages_to_gemini_user_input(messages)
    assert result == "last question"


def test_messages_to_gemini_user_input_empty(provider):
    result = provider._messages_to_gemini_user_input([])
    assert result == ""


# ---------------------------------------------------------------------------
# _process_messages_for_gemini() helper — ModelMessage -> SDK contents
# ---------------------------------------------------------------------------

def test_process_messages_for_gemini_maps_roles_and_skips_system(provider):
    messages = [
        ModelMessage(role="system", content="sys"),
        ModelMessage(role="user", content="hello"),
        ModelMessage(role="assistant", content="hi there"),
    ]
    contents = provider._process_messages_for_gemini(messages)
    assert contents == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "assistant", "parts": [{"text": "hi there"}]},
    ]


def test_process_messages_for_gemini_preserves_multimodal_list_content(provider):
    parts = [{"text": "describe"}, {"file_data": {"file_uri": "x"}}]
    contents = provider._process_messages_for_gemini(
        [ModelMessage(role="user", content=parts)]
    )
    assert contents == [{"role": "user", "parts": parts}]


def test_process_messages_for_gemini_stringifies_other_content(provider):
    contents = provider._process_messages_for_gemini(
        [ModelMessage(role="user", content=123)]
    )
    assert contents == [{"role": "user", "parts": [{"text": "123"}]}]


# ---------------------------------------------------------------------------
# _extract_system() helper
# ---------------------------------------------------------------------------

def test_extract_system_prefers_explicit_instruction(provider):
    messages = [ModelMessage(role="system", content="from_msg")]
    result = provider._extract_system(messages, "explicit")
    assert result == "explicit"


def test_extract_system_falls_back_to_system_message(provider):
    messages = [ModelMessage(role="system", content="from_msg")]
    result = provider._extract_system(messages, None)
    assert result == "from_msg"


def test_extract_system_none_when_missing(provider):
    messages = [ModelMessage(role="user", content="hi")]
    result = provider._extract_system(messages, None)
    assert result is None
