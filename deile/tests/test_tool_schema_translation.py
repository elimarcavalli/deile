"""Tests: ToolSchema cross-provider translation — Phase 2."""

from __future__ import annotations

import pytest

from deile.tools.base import SecurityLevel, ToolCategory, ToolSchema
from deile.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_schema() -> ToolSchema:
    return ToolSchema(
        name="bash",
        description="Run a shell command",
        parameters={
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "Command to run"},
            },
        },
        required=["command"],
        security_level=SecurityLevel.MODERATE,
        category=ToolCategory.EXECUTION,
    )


@pytest.fixture
def complex_schema() -> ToolSchema:
    return ToolSchema(
        name="read_file",
        description="Read file contents",
        parameters={
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "File path"},
                "encoding": {"type": "STRING", "description": "Encoding"},
                "max_lines": {"type": "INTEGER", "description": "Max lines"},
            },
        },
        required=["path"],
        security_level=SecurityLevel.SAFE,
        category=ToolCategory.FILE,
    )


# ---------------------------------------------------------------------------
# Anthropic format tests
# ---------------------------------------------------------------------------

def test_to_anthropic_tool_structure(simple_schema):
    tool = simple_schema.to_anthropic_tool()
    assert tool["name"] == "bash"
    assert tool["description"] == "Run a shell command"
    assert "input_schema" in tool
    assert "name" not in tool["input_schema"]  # schema only, no name nesting


def test_to_anthropic_tool_required_propagated(simple_schema):
    tool = simple_schema.to_anthropic_tool()
    assert tool["input_schema"]["required"] == ["command"]


def test_to_anthropic_tool_type_lowercased(simple_schema):
    tool = simple_schema.to_anthropic_tool()
    assert tool["input_schema"]["type"] == "object"
    assert tool["input_schema"]["properties"]["command"]["type"] == "string"


# ---------------------------------------------------------------------------
# OpenAI format tests
# ---------------------------------------------------------------------------

def test_to_openai_function_structure(simple_schema):
    fn = simple_schema.to_openai_function()
    assert fn["type"] == "function"
    assert "function" in fn
    inner = fn["function"]
    assert inner["name"] == "bash"
    assert inner["description"] == "Run a shell command"
    assert "parameters" in inner
    assert inner["strict"] is False


def test_to_openai_function_required_propagated(simple_schema):
    fn = simple_schema.to_openai_function()
    assert fn["function"]["parameters"]["required"] == ["command"]


def test_to_openai_function_type_lowercased(complex_schema):
    fn = complex_schema.to_openai_function()
    params = fn["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["max_lines"]["type"] == "integer"


# ---------------------------------------------------------------------------
# Cross-format consistency
# ---------------------------------------------------------------------------

def test_parameters_json_schema_identical_across_formats(complex_schema):
    """The normalised parameters dict should be the same regardless of provider format."""
    anthropic_params = complex_schema.to_anthropic_tool()["input_schema"]
    openai_params = complex_schema.to_openai_function()["function"]["parameters"]

    # Strip required from both before comparing (it is always appended)
    def _core(p):
        return {k: v for k, v in p.items() if k != "required"}

    assert _core(anthropic_params) == _core(openai_params)


# ---------------------------------------------------------------------------
# ToolRegistry methods
# ---------------------------------------------------------------------------

class _FakeSchemaOnlyTool:
    """Minimal Tool-like object to exercise registry translation methods."""

    def __init__(self, schema: ToolSchema):
        self._schema = schema
        self.name = schema.name
        self.category = schema.category.value
        self.is_enabled = True

    @property
    def schema(self):
        return self._schema


def _build_registry(*schemas: ToolSchema) -> ToolRegistry:
    from deile.tools.base import Tool, ToolContext, ToolResult, ToolStatus

    class _MinimalTool(Tool):
        def __init__(self, s: ToolSchema):
            super().__init__(schema=s)
            self._name = s.name
            self._cat = s.category.value

        @property
        def name(self):
            return self._name

        @property
        def description(self):
            return self._schema.description

        @property
        def category(self):
            return self._cat

        async def execute(self, context: ToolContext) -> ToolResult:
            return ToolResult(status=ToolStatus.SUCCESS)

    registry = ToolRegistry()
    for s in schemas:
        registry.register(_MinimalTool(s))
    return registry


def test_registry_anthropic_equals_gemini_count(simple_schema, complex_schema):
    registry = _build_registry(simple_schema, complex_schema)
    assert len(registry.get_anthropic_tools()) == len(registry.get_gemini_functions())


def test_registry_openai_equals_gemini_count(simple_schema, complex_schema):
    registry = _build_registry(simple_schema, complex_schema)
    assert len(registry.get_openai_functions()) == len(registry.get_gemini_functions())


def test_registry_anthropic_format(simple_schema):
    registry = _build_registry(simple_schema)
    tools = registry.get_anthropic_tools()
    assert len(tools) == 1
    assert "input_schema" in tools[0]


def test_registry_openai_format(simple_schema):
    registry = _build_registry(simple_schema)
    fns = registry.get_openai_functions()
    assert len(fns) == 1
    assert fns[0]["type"] == "function"
