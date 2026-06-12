"""Pin the inline ``ToolSchema`` of ``python_execute`` and ``pip_install``.

Issue #651 migrated both tools from a JSON schema file in
``deile/tools/schemas/`` to an inline ``ToolSchema`` declared in
``__init__`` (mirroring the rest of the roster). These tests guard the two
properties that motivated the migration plus the observable contract that
must NOT change:

* a ``ToolRegistry`` that only ran ``auto_discover()`` — without
  ``load_schemas_from_directory()`` — already exposes both tools to the
  three function-calling exporters (the fragility the issue closes);
* parameters, required fields, security level, category and both
  descriptions match what the legacy JSON loader produced.
"""
from __future__ import annotations

import pytest

from deile.tools.base import SecurityLevel, ToolCategory
from deile.tools.execution_tools import PipInstallTool, PythonExecutionTool
from deile.tools.registry import ToolRegistry

_INLINE_TOOLS = {
    "python_execute": (PythonExecutionTool, ["code"], {"code", "timeout"}),
    "pip_install": (
        PipInstallTool,
        ["package"],
        {
            "package",
            "version",
            "update_requirements",
            "requirements_file",
            "upgrade",
            "timeout",
        },
    ),
}


@pytest.mark.parametrize("name", list(_INLINE_TOOLS))
def test_inline_schema_present_without_json_loader(name):
    """AC1: discovered tools carry a schema even without the JSON loader."""
    registry = ToolRegistry()
    registry.auto_discover()  # intentionally NOT load_schemas_from_directory

    tool = registry.get(name)
    assert tool is not None
    assert tool.schema is not None, (
        f"{name} has no schema after auto_discover() alone — regression to "
        "the JSON-only path that #651 removed"
    )


@pytest.mark.parametrize("name", list(_INLINE_TOOLS))
def test_inline_schema_metadata(name):
    cls, _required, _props = _INLINE_TOOLS[name]
    schema = cls().schema

    assert schema.name == name
    assert schema.security_level is SecurityLevel.MODERATE
    assert schema.category is ToolCategory.EXECUTION
    # Mirrors the legacy JSON loader: ``required`` lived nested inside
    # ``parameters`` (top-level absent → ToolSchema.required stays empty).
    assert schema.required == []
    assert schema.parameters["required"] == _required
    assert set(schema.parameters["properties"]) == _props


@pytest.mark.parametrize("name", list(_INLINE_TOOLS))
def test_inline_schema_anthropic_export(name):
    cls, required, props = _INLINE_TOOLS[name]
    exported = cls().schema.to_anthropic_tool()

    assert exported["name"] == name
    input_schema = exported["input_schema"]
    assert input_schema["type"] == "object"  # converter lowercases OBJECT
    assert input_schema["required"] == required
    assert set(input_schema["properties"]) == props
    for spec in input_schema["properties"].values():
        # uppercase STRING/NUMBER/BOOLEAN normalized by the converter
        assert spec["type"] in {"string", "number", "boolean"}


@pytest.mark.parametrize("name", list(_INLINE_TOOLS))
def test_inline_schema_openai_export(name):
    cls, required, props = _INLINE_TOOLS[name]
    fn = cls().schema.to_openai_function()["function"]

    assert fn["name"] == name
    assert fn["parameters"]["required"] == required
    assert set(fn["parameters"]["properties"]) == props


@pytest.mark.parametrize("name", list(_INLINE_TOOLS))
def test_inline_schema_gemini_export(name):
    cls, required, _props = _INLINE_TOOLS[name]
    try:
        declaration = cls().schema.to_gemini_function()
    except ImportError:
        pytest.skip("google.genai SDK not installed")
    assert declaration.name == name


def test_python_execute_descriptions_preserved():
    """The short ``description`` property (consumed by ``/tools``) and the
    rich schema description (sent to the LLM) stay distinct, as before."""
    tool = PythonExecutionTool()
    assert tool.description == (
        "Executes a Python snippet in a subprocess and returns "
        "stdout/stderr/exit_code"
    )
    assert tool.description != tool.schema.description
    assert "session's working directory" in tool.schema.description


def test_pip_install_descriptions_preserved():
    tool = PipInstallTool()
    assert tool.description.startswith("Installs a Python package via pip and")
    assert tool.description != tool.schema.description
    assert "Idempotent" in tool.schema.description
