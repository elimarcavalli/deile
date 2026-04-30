"""Integration E2E: Anthropic provider — plain chat + tool calling.

Skipped if ANTHROPIC_API_KEY is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
pytestmark = pytest.mark.skipif(
    not ANTHROPIC_API_KEY,
    reason="ANTHROPIC_API_KEY not set — skipping Anthropic tool E2E test",
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_YAML_PATH = Path(__file__).parents[3] / "deile" / "config" / "model_providers.yaml"
_MODEL_ID = "claude-haiku-4-5"      # cheapest Anthropic in the catalog
_PROVIDER_ID = "anthropic"


def _make_provider():
    """Build an AnthropicProvider from the real catalog, no router involved."""
    from deile.core.models.catalog import ModelCatalog
    from deile.core.models.anthropic_provider import AnthropicProvider
    from deile.core.models.provider_config import ProviderConfig

    catalog = ModelCatalog.from_yaml(_YAML_PATH)
    handle = catalog.get(_PROVIDER_ID, _MODEL_ID)
    config = ProviderConfig(
        provider_id=_PROVIDER_ID,
        api_key_env="ANTHROPIC_API_KEY",
        base_url=None,
        sdk_kwargs={},
    )
    return AnthropicProvider(handle, config)


def _make_echo_registry():
    """Return a fake ToolRegistry that exposes a single 'echo' tool."""
    from deile.tools.base import Tool, ToolContext, ToolResult, ToolStatus, ToolSchema

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
        def get(self, tool_name: str):  # noqa: ANN001
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
async def test_anthropic_plain_chat():
    """Plain generate() call — assert '4' is in the reply."""
    from deile.core.models.base import ModelMessage

    provider = _make_provider()
    msgs = [ModelMessage(role="user", content="What is 2+2? Reply with just the number.")]
    response = await provider.generate(msgs)

    assert "4" in response.content, f"Unexpected response: {response.content!r}"
    assert response.usage.prompt_tokens > 0


@pytest.mark.integration
async def test_anthropic_tool_calling():
    """chat_with_tools() — model must call 'echo' with 'hello world'."""
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
        "deile.core.models.anthropic_provider.get_tool_registry",
        return_value=fake_registry,
    ):
        _text, tool_results, usage = await provider.chat_with_tools(
            messages=msgs,
            tools=[echo_schema],
        )

    assert tool_results, "Expected at least one tool result; got none"
    # The echo tool returns the text in data or message
    combined = " ".join(
        str(tr.data) + " " + str(tr.message)
        for tr in tool_results
    )
    assert "hello world" in combined.lower(), (
        f"'hello world' not found in tool results: {combined!r}"
    )
    assert usage.total_tokens > 0
