"""Integration E2E: Gemini provider — plain chat + tool calling.

Skipped if GOOGLE_API_KEY is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not GOOGLE_API_KEY,
        reason="GOOGLE_API_KEY not set — skipping Gemini tool E2E test",
    ),
]

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_YAML_PATH = Path(__file__).parents[3] / "deile" / "config" / "model_providers.yaml"
_MODEL_ID = "gemini-2.0-flash"   # stable, widely available Gemini model
_PROVIDER_ID = "gemini"


def _make_provider():
    """Build a GeminiProvider from a ModelHandle directly (catalog model
    names for Gemini are speculative/preview — use a known-working ID)."""
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.gemini_provider import GeminiProvider
    from deile.core.models.provider_config import ProviderConfig
    from deile.core.models.tier import ModelTier

    handle = ModelHandle(
        provider_id=_PROVIDER_ID,
        model_id=_MODEL_ID,
        tier=ModelTier.TIER_3,
        pricing=ModelPricing(input_per_1m_usd=0.15, output_per_1m_usd=0.60),
        context_window=128000,
        capabilities=frozenset(["function_calling", "streaming", "vision"]),
        display_name="Gemini 2.0 Flash",
        label="fast",
    )
    config = ProviderConfig(
        provider_id=_PROVIDER_ID,
        api_key_env="GOOGLE_API_KEY",
        base_url=None,
        sdk_kwargs={},
    )
    return GeminiProvider(handle, config)


def _make_echo_registry():
    """Return a fake ToolRegistry that exposes a single 'echo' tool."""
    from deile.tools.base import (Tool, ToolContext, ToolResult, ToolSchema,
                                  ToolStatus)

    class EchoTool(Tool):
        """Returns its input text verbatim."""

        @property
        def name(self) -> str:
            return "echo"

        @property
        def description(self) -> str:
            return "Echo the input text back exactly as given."

        @property
        def category(self) -> str:
            return "other"

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name="echo",
                description="Echo the input text back exactly as given.",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to echo"},
                    },
                    "required": ["text"],
                },
            )

        async def execute(self, context: ToolContext) -> ToolResult:
            text = context.parsed_args.get("text", "")
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data={"result": text},
                message=text,
            )

    class _FakeRegistry:
        def get(self, tool_name: str):
            if tool_name == "echo":
                return EchoTool()
            return None

        @property
        def _tools(self):
            return {"echo": EchoTool()}

    return _FakeRegistry()


def _echo_tool_schema():
    from deile.tools.base import ToolSchema

    return ToolSchema(
        name="echo",
        description="Echo the input text back exactly as given.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo"},
            },
            "required": ["text"],
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gemini_plain_chat():
    """Plain generate() call — assert '4' is in the reply."""
    from deile.core.models.base import ModelMessage

    provider = _make_provider()
    msgs = [ModelMessage(role="user", content="What is 2+2? Reply with just the number.")]
    response = await provider.generate(msgs)

    assert "4" in response.content, f"Unexpected response: {response.content!r}"
    assert response.usage.prompt_tokens > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gemini_tool_calling():
    """chat_with_tools() — model must call 'echo' with 'hello world'.

    GeminiProvider uses the manual function calling loop via _gemini_chat_with_tools,
    which calls get_tool_registry from deile.tools.registry.
    """
    from deile.core.models.base import ModelMessage

    provider = _make_provider()
    echo_schema = _echo_tool_schema()
    fake_registry = _make_echo_registry()

    msgs = [
        ModelMessage(
            role="user",
            content="Please call the echo tool with text='hello world'.",
        )
    ]

    with patch(
        "deile.tools.registry.get_tool_registry",
        return_value=fake_registry,
    ):
        _text, tool_results, usage = await provider.chat_with_tools(
            messages=msgs,
            tools=[echo_schema],
        )

    assert tool_results, "Expected at least one tool result; got none"
    combined = " ".join(
        str(tr.data) + " " + str(tr.message)
        for tr in tool_results
    )
    assert "hello world" in combined.lower(), (
        f"'hello world' not found in tool results: {combined!r}"
    )
