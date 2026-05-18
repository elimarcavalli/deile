"""Tests: deile.tools.schema_export — multi-provider schema export.

Covers the authorization/security-level filtering extracted from
``ToolRegistry`` (PR #221) and the cron-tool helpers, which were only
exercised indirectly before.
"""

from __future__ import annotations

from deile.tools import schema_export
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema, ToolStatus)
from deile.tools.cron_create_tool import _resolve_schedule
from deile.tools.cron_tool_base import unexpected_error


class _MinimalTool(Tool):
    """Smallest Tool that carries a real ToolSchema for export tests."""

    def __init__(self, schema: ToolSchema):
        super().__init__(schema=schema)
        self._name = schema.name
        self._cat = schema.category.value

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._schema.description

    @property
    def category(self) -> str:
        return self._cat

    async def execute(self, context: ToolContext) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS)


def _tool(name: str, level: SecurityLevel) -> _MinimalTool:
    return _MinimalTool(
        ToolSchema(
            name=name,
            description=f"{name} description",
            parameters={"type": "OBJECT", "properties": {}},
            required=[],
            security_level=level,
            category=ToolCategory.OTHER,
        )
    )


# ---------------------------------------------------------------------------
# is_security_level_allowed
# ---------------------------------------------------------------------------

def test_security_level_allowed_within_max():
    assert schema_export.is_security_level_allowed(
        SecurityLevel.SAFE, SecurityLevel.MODERATE
    )
    assert schema_export.is_security_level_allowed(
        SecurityLevel.MODERATE, SecurityLevel.MODERATE
    )


def test_security_level_disallowed_above_max():
    assert not schema_export.is_security_level_allowed(
        SecurityLevel.DANGEROUS, SecurityLevel.SAFE
    )
    assert not schema_export.is_security_level_allowed(
        SecurityLevel.MODERATE, SecurityLevel.SAFE
    )


# ---------------------------------------------------------------------------
# iter_authorized_tools
# ---------------------------------------------------------------------------

def test_iter_authorized_only_filters_disabled():
    tools = {"a": _tool("a", SecurityLevel.SAFE), "b": _tool("b", SecurityLevel.SAFE)}
    names = {t.name for t in schema_export.iter_authorized_tools(
        tools, enabled={"a"}, authorized_only=True, security_level=None
    )}
    assert names == {"a"}


def test_iter_authorized_only_false_keeps_disabled():
    tools = {"a": _tool("a", SecurityLevel.SAFE), "b": _tool("b", SecurityLevel.SAFE)}
    names = {t.name for t in schema_export.iter_authorized_tools(
        tools, enabled=set(), authorized_only=False, security_level=None
    )}
    assert names == {"a", "b"}


def test_iter_security_level_filters_above_max():
    tools = {
        "safe": _tool("safe", SecurityLevel.SAFE),
        "danger": _tool("danger", SecurityLevel.DANGEROUS),
    }
    names = {t.name for t in schema_export.iter_authorized_tools(
        tools, enabled={"safe", "danger"},
        authorized_only=True, security_level=SecurityLevel.SAFE,
    )}
    assert names == {"safe"}


# ---------------------------------------------------------------------------
# per-provider exporters
# ---------------------------------------------------------------------------

def test_exporters_agree_on_count():
    tools = {"a": _tool("a", SecurityLevel.SAFE), "b": _tool("b", SecurityLevel.SAFE)}
    enabled = {"a", "b"}
    assert len(schema_export.get_anthropic_tools(tools, enabled)) == 2
    assert len(schema_export.get_openai_functions(tools, enabled)) == 2
    assert len(schema_export.get_gemini_functions(tools, enabled)) == 2


def test_exporters_respect_security_level():
    tools = {
        "safe": _tool("safe", SecurityLevel.SAFE),
        "danger": _tool("danger", SecurityLevel.DANGEROUS),
    }
    enabled = {"safe", "danger"}
    assert len(schema_export.get_anthropic_tools(
        tools, enabled, security_level=SecurityLevel.SAFE
    )) == 1
    assert len(schema_export.get_openai_functions(
        tools, enabled, security_level=SecurityLevel.SAFE
    )) == 1


# ---------------------------------------------------------------------------
# cron_tool_base.unexpected_error
# ---------------------------------------------------------------------------

def test_unexpected_error_shape():
    exc = RuntimeError("boom")
    result = unexpected_error(exc)
    assert not result.is_success
    assert result.metadata["error_code"] == "UNEXPECTED"
    assert "RuntimeError" in result.message
    assert result.error is exc


# ---------------------------------------------------------------------------
# cron_create_tool._resolve_schedule
# ---------------------------------------------------------------------------

def test_resolve_schedule_cron_only_passthrough():
    cron, run_at, error = _resolve_schedule(None, "*/5 * * * *", None)
    assert cron == "*/5 * * * *"
    assert run_at is None
    assert error is None


def test_resolve_schedule_invalid_when():
    cron, run_at, error = _resolve_schedule("florble glorp", None, None)
    assert cron is None
    assert run_at is None
    assert error is not None
    assert error.metadata["error_code"] == "INVALID_WHEN"


def test_resolve_schedule_invalid_run_at():
    cron, run_at, error = _resolve_schedule(None, None, "banana")
    assert cron is None
    assert run_at is None
    assert error is not None
    assert error.metadata["error_code"] == "INVALID_DATETIME"
