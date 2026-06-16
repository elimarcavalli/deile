"""Unit tests for the shared ``resolve_and_execute_tool`` helper.

The helper performs the resolve/not-found/execute/exception-wrap step shared by
all three concrete providers; only the payload formatting stays per-provider.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from deile.core.models.tool_execution import (
    OUTCOME_EXCEPTION,
    OUTCOME_NOT_FOUND,
    OUTCOME_RAN,
    build_tool_result_payload,
    payload_to_text,
    resolve_and_execute_tool,
)
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

    def list_names(self):
        return sorted(self._tools.keys())


@pytest.fixture
def install_registry(monkeypatch):
    def _install(registry):
        monkeypatch.setattr("deile.tools.registry.get_tool_registry", lambda: registry)
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
    install_registry(_FakeRegistry({"t": _FakeTool("t", exc=asyncio.CancelledError())}))

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


class TestPayloadToText:
    """``payload_to_text`` — the serializer shared by the Anthropic/OpenAI payloads."""

    def test_str_passes_through_unchanged(self):
        assert payload_to_text("already text") == "already text"

    def test_dict_is_json_encoded(self):
        assert payload_to_text({"a": 1, "b": [2, 3]}) == '{"a": 1, "b": [2, 3]}'

    def test_non_serializable_leaf_uses_default_str(self):
        sentinel = object()
        payload = {"k": sentinel}
        assert payload_to_text(payload) == json.dumps(payload, default=str)

    def test_circular_reference_falls_back_to_str(self):
        circular = {}
        circular["self"] = circular
        assert payload_to_text(circular) == str(circular)


class TestBuildToolResultPayload:
    """Wire-format regression for the per-provider payload builder.

    Anthropic and OpenAI both wrap a ``ToolResult`` via this helper; the
    byte shape of each payload is observed by downstream tests and by the
    real provider clients, so divergences must be intentional. The cases
    below document the contract post-#238 (issue 7 in the refactor):
    success carries both ``result`` and (optionally) ``message`` even when
    empty; error fallback is ``f"{name} failed"``; not-found and
    exception outcomes collapse to a flat ``{"error", "status"}`` shape.
    """

    def test_success_minimal_shape(self):
        result = ToolResult(status=ToolStatus.SUCCESS, message="ok", data="payload")
        out = build_tool_result_payload(result, OUTCOME_RAN, "t")
        assert out == {"status": "success", "result": "payload"}

    def test_success_with_include_message_keeps_empty_message_key(self):
        # OpenAI flag: ``message`` is always present (even empty) so the
        # provider's downstream JSON shape stays stable.
        result = ToolResult(status=ToolStatus.SUCCESS, message="", data="payload")
        out = build_tool_result_payload(result, OUTCOME_RAN, "t", include_message=True)
        assert out == {"status": "success", "result": "payload", "message": ""}

    def test_success_with_none_data_serialises_to_empty_string(self):
        result = ToolResult(status=ToolStatus.SUCCESS, message="ok", data=None)
        out = build_tool_result_payload(result, OUTCOME_RAN, "t")
        assert out["result"] == ""

    def test_tool_reported_error_uses_message(self):
        result = ToolResult(status=ToolStatus.ERROR, message="boom", data="x")
        out = build_tool_result_payload(result, OUTCOME_RAN, "t")
        assert out == {"status": "error", "error": "boom"}

    def test_tool_reported_error_fallback_message_uses_name(self):
        # No explicit message → fallback is ``f"{name} failed"``.
        result = ToolResult(status=ToolStatus.ERROR, message="", data=None)
        out = build_tool_result_payload(result, OUTCOME_RAN, "my_tool")
        assert out == {"status": "error", "error": "my_tool failed"}

    def test_tool_reported_error_with_include_data_carries_result(self):
        result = ToolResult(status=ToolStatus.ERROR, message="boom", data="ctx")
        out = build_tool_result_payload(
            result, OUTCOME_RAN, "t", include_data_on_error=True
        )
        assert out == {"status": "error", "error": "boom", "result": "ctx"}

    def test_not_found_outcome_returns_flat_error_shape(self):
        result = ToolResult(status=ToolStatus.ERROR, message="no such tool", data=None)
        out = build_tool_result_payload(result, OUTCOME_NOT_FOUND, "ghost")
        assert out == {"error": "no such tool", "status": "error"}

    def test_exception_outcome_returns_flat_error_shape(self):
        result = ToolResult(status=ToolStatus.ERROR, message="kaboom", data=None)
        out = build_tool_result_payload(result, OUTCOME_EXCEPTION, "t")
        assert out == {"error": "kaboom", "status": "error"}
