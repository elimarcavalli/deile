"""Regression tests for the tooling-hardening audit.

Covers three concrete fixes:

1. ``TestRunnerTool`` (``run_tests``) now carries a ``ToolSchema`` so it is
   exported to every LLM function-calling provider — previously it was
   registered but invisible (schema-less tools are skipped by the exporters)
   even though ``validation_gate`` treats ``run_tests`` as a real tool.
2. ``BashExecuteTool`` returns a clear error for an invalid ``security_level``
   instead of leaking ``ValueError: 'x' is not in list``.
3. ``SearchTool`` returns a clean "cannot be empty" message for a null
   ``query`` instead of an ``AttributeError`` on ``None.strip()``.
"""

from __future__ import annotations

from deile.tools.base import ToolContext, ToolSchema
from deile.tools.bash_tool import BashExecuteTool
from deile.tools.execution_tools import TestRunnerTool
from deile.tools.search_tool import SearchTool

# ---------------------------------------------------------------------------
# 1. run_tests is now function-callable
# ---------------------------------------------------------------------------


def test_run_tests_has_schema():
    tool = TestRunnerTool()
    assert tool.schema is not None
    assert tool.schema.name == "run_tests"


def test_run_tests_schema_converts_to_all_providers():
    """The schema must translate cleanly to all three provider formats."""
    schema = TestRunnerTool().schema
    assert isinstance(schema, ToolSchema)

    anthropic = schema.to_anthropic_tool()
    assert anthropic["name"] == "run_tests"
    assert anthropic["input_schema"]["type"] == "object"

    openai = schema.to_openai_function()
    assert openai["function"]["name"] == "run_tests"

    # Gemini conversion is import-guarded; only assert when the SDK is present.
    try:
        gemini = schema.to_gemini_function()
    except ImportError:
        gemini = None
    if gemini is not None:
        assert gemini.name == "run_tests"


def test_run_tests_schema_test_type_enum():
    props = TestRunnerTool().schema.parameters["properties"]
    assert set(props["test_type"]["enum"]) == {"pytest", "unittest", "nose"}


def test_run_tests_exported_by_registry():
    """A registry with run_tests registered exports it to every provider."""
    from deile.tools.base import Tool
    from deile.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(TestRunnerTool())
    assert isinstance(registry.get("run_tests"), Tool)
    assert any(t["name"] == "run_tests" for t in registry.get_anthropic_tools())
    assert any(
        f["function"]["name"] == "run_tests"
        for f in registry.get_openai_functions()
    )


# ---------------------------------------------------------------------------
# 2. bash_execute rejects an invalid security_level cleanly
# ---------------------------------------------------------------------------


async def test_bash_invalid_security_level_is_clean_error(tmp_path):
    tool = BashExecuteTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={
            "command": "echo hi",
            "working_directory": str(tmp_path),
            "security_level": "high",  # not a valid level
        },
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert "invalid security_level" in result.message.lower()
    assert "'high'" in result.message


async def test_bash_valid_security_level_still_runs(tmp_path):
    tool = BashExecuteTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={
            "command": "echo hardening",
            "working_directory": str(tmp_path),
            "security_level": "safe",
        },
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert result.data["exit_code"] == 0


# ---------------------------------------------------------------------------
# 3. find_in_files handles a null query without AttributeError
# ---------------------------------------------------------------------------


async def test_search_null_query_is_clean_error(tmp_path):
    tool = SearchTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"query": None, "path": str(tmp_path)},
        working_directory=str(tmp_path),
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert "cannot be empty" in result.message.lower()


async def test_search_empty_query_is_clean_error(tmp_path):
    tool = SearchTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"query": "   ", "path": str(tmp_path)},
        working_directory=str(tmp_path),
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert "cannot be empty" in result.message.lower()


async def test_search_valid_query_still_matches(tmp_path):
    (tmp_path / "f.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
    tool = SearchTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"query": "needle", "path": str(tmp_path)},
        working_directory=str(tmp_path),
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert result.data["total_matches"] >= 1
