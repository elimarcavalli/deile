"""Unit tests for the shared ``resolve_and_execute_tool`` helper.

The helper performs the resolve/not-found/execute/exception-wrap step shared by
all three concrete providers; only the payload formatting stays per-provider.
"""

from __future__ import annotations

import asyncio

import pytest

from deile.core.models.tool_execution import (OUTCOME_EXCEPTION,
                                              OUTCOME_NOT_FOUND, OUTCOME_RAN,
                                              resolve_and_execute_tool)
from deile.tools.base import ToolResult, ToolStatus


class _FakeTool:
    def __init__(self, name="fake_tool", *, exc=None, result=None):
        self.name = name
        self._exc = exc
        self._result = result

    async def execute(self, ctx):
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeRegistry:
    def __init__(self, tools=None):
        self._tools = dict(tools or {})

    def get(self, name):
        return self._tools.get(name)


@pytest.fixture
def install_registry(monkeypatch):
    def _install(registry):
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )
        return registry
    return _install


def _ctx_factory(name, args, tool):
    return {"name": name, "args": args, "tool": tool}


async def test_resolve_runs_tool_and_returns_its_result(install_registry):
    expected = ToolResult(status=ToolStatus.SUCCESS, message="ok", data=1)
    install_registry(_FakeRegistry({"t": _FakeTool("t", result=expected)}))

    result, outcome = await resolve_and_execute_tool(
        name="t",
        args={"x": 1},
        not_found_message_fn=lambda n, avail: f"missing {n}",
        context_factory=_ctx_factory,
    )

    assert outcome == OUTCOME_RAN
    assert result is expected


async def test_resolve_passes_resolved_tool_to_context_factory(install_registry):
    """The resolved tool reaches the context factory so a provider can stamp
    the canonical ``tool.name`` even when the model called an alias."""
    tool = _FakeTool("canonical_name")
    captured = {}

    def _factory(name, args, resolved_tool):
        captured["tool"] = resolved_tool
        return {}

    install_registry(_FakeRegistry({"alias": tool}))

    await resolve_and_execute_tool(
        name="alias",
        args={},
        not_found_message_fn=lambda n, avail: "x",
        context_factory=_factory,
    )

    assert captured["tool"] is tool


async def test_resolve_tool_not_found(install_registry):
    install_registry(_FakeRegistry({"other": _FakeTool("other")}))

    result, outcome = await resolve_and_execute_tool(
        name="ghost",
        args={},
        not_found_message_fn=lambda n, avail: f"no {n}; have {avail}",
        context_factory=_ctx_factory,
        not_found_metadata={"error_code": "NOPE"},
    )

    assert outcome == OUTCOME_NOT_FOUND
    assert result.status == ToolStatus.ERROR
    assert result.message == "no ghost; have ['other']"
    assert result.metadata == {"error_code": "NOPE"}


async def test_resolve_tool_raises_exception(install_registry):
    boom = ValueError("kaboom")
    install_registry(_FakeRegistry({"t": _FakeTool("t", exc=boom)}))

    result, outcome = await resolve_and_execute_tool(
        name="t",
        args={},
        not_found_message_fn=lambda n, avail: "x",
        context_factory=_ctx_factory,
        exception_message_fn=lambda n, exc: f"{n} failed: {exc}",
        exception_metadata={"function_name": "t"},
    )

    assert outcome == OUTCOME_EXCEPTION
    assert result.status == ToolStatus.ERROR
    assert result.error is boom
    assert result.message == "t failed: kaboom"
    assert result.metadata == {"function_name": "t"}


async def test_resolve_does_not_swallow_cancelled_error(install_registry):
    """``except Exception`` must NOT catch ``asyncio.CancelledError`` — it is a
    ``BaseException`` and has to propagate so cancellation is honoured."""
    install_registry(
        _FakeRegistry({"t": _FakeTool("t", exc=asyncio.CancelledError())})
    )

    with pytest.raises(asyncio.CancelledError):
        await resolve_and_execute_tool(
            name="t",
            args={},
            not_found_message_fn=lambda n, avail: "x",
            context_factory=_ctx_factory,
        )


async def test_resolve_registry_without_get_or_tools(install_registry):
    """A registry exposing neither ``get`` nor ``_tools`` resolves to
    not-found with an empty available list rather than raising."""
    class _BareRegistry:
        pass

    install_registry(_BareRegistry())

    result, outcome = await resolve_and_execute_tool(
        name="t",
        args={},
        not_found_message_fn=lambda n, avail: f"avail={avail}",
        context_factory=_ctx_factory,
    )

    assert outcome == OUTCOME_NOT_FOUND
    assert result.message == "avail=[]"
