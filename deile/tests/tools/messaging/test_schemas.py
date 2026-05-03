"""Each messaging tool produces valid Anthropic / OpenAI / Gemini schemas."""

from __future__ import annotations

import pytest

from deile.tools.messaging import (
    DiscordGetUserProfileTool,
    DiscordMentionRoleTool,
    DiscordPinMessageTool,
    DiscordReactTool,
    DiscordSendDMTool,
    DiscordSendMessageTool,
    DiscordStartThreadTool,
)


ALL_TOOLS = [
    DiscordSendMessageTool,
    DiscordSendDMTool,
    DiscordReactTool,
    DiscordStartThreadTool,
    DiscordPinMessageTool,
    DiscordMentionRoleTool,
    DiscordGetUserProfileTool,
]


@pytest.mark.parametrize("cls", ALL_TOOLS)
def test_anthropic_schema(cls):
    tool = cls()
    schema = tool.schema.to_anthropic_tool()
    assert schema["name"] == tool.name
    assert isinstance(schema["description"], str) and schema["description"]
    assert schema["input_schema"]["type"] == "object"
    assert "properties" in schema["input_schema"]


@pytest.mark.parametrize("cls", ALL_TOOLS)
def test_openai_function_schema(cls):
    tool = cls()
    schema = tool.schema.to_openai_function()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == tool.name
    assert "parameters" in fn


@pytest.mark.parametrize("cls", ALL_TOOLS)
def test_gemini_function_declaration(cls):
    """Gemini SDK turns the schema into a FunctionDeclaration object."""
    tool = cls()
    decl = tool.schema.to_gemini_function()
    # The GenAI SDK FunctionDeclaration is a pydantic model; verify name/description.
    assert getattr(decl, "name", None) == tool.name
    assert getattr(decl, "description", None) == tool.schema.description


@pytest.mark.parametrize("cls", ALL_TOOLS)
def test_required_params_match_signature(cls):
    """Required params must be declared on the schema."""
    tool = cls()
    for req in tool.schema.required:
        assert req in tool.schema.parameters["properties"]


def test_categories_are_messaging():
    from deile.tools.base import ToolCategory

    for cls in ALL_TOOLS:
        tool = cls()
        assert tool.schema.category == ToolCategory.MESSAGING
        assert tool.category == "messaging"
