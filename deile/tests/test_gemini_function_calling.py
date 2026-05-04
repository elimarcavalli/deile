"""Regression tests for manual function calling on GeminiProvider.

These tests cover the fix for two production bugs:

1. **KeyError on hallucinated / wrongly-registered tool names**: with the legacy
   AFC-based path the SDK looked tools up by ``__name__`` against a function map
   and raised ``KeyError`` on mismatch, surfacing as
   ``"I encountered an error during function calling: 'run_code'"``.

2. **Lost chat history after tool errors**: when ``chat.send_message`` raised
   inside the SDK's AFC, ``record_history`` was skipped and the next turn
   started with no memory of the failed turn — the model would respond as if
   nothing had happened.

The new ``chat_with_tools`` loop performs manual function calling, so failures
become regular ``function_response`` parts and never crash ``send_message``.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from deile.core.models.gemini_provider import GeminiProvider, _stringify_for_model
from deile.tools.base import ToolResult, ToolStatus


def _make_function_call_part(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function_call=SimpleNamespace(name=name, args=args),
        text=None,
    )


def _make_text_part(text: str) -> SimpleNamespace:
    return SimpleNamespace(function_call=None, text=text)


def _make_response(*parts: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))]
    )


class _FakeChat:
    """Minimal stand-in for ``google.genai.chats.Chat`` used by the loop."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.sent: list[Any] = []

    def send_message(self, message: Any) -> SimpleNamespace:
        self.sent.append(message)
        if not self._responses:
            return _make_response(_make_text_part(""))
        return self._responses.pop(0)


@pytest.fixture
def provider() -> GeminiProvider:
    """A bare GeminiProvider instance bypassing __init__ side-effects.

    We don't need the SDK client or config — only the methods under test.
    """
    return GeminiProvider.__new__(GeminiProvider)


class TestStringifyForModel:
    def test_passes_primitives_through(self) -> None:
        assert _stringify_for_model("x") == "x"
        assert _stringify_for_model(1) == 1
        assert _stringify_for_model(1.5) == 1.5
        assert _stringify_for_model(True) is True
        assert _stringify_for_model(None) is None

    def test_recurses_into_dict_and_list(self) -> None:
        result = _stringify_for_model({"a": [1, 2], "b": {"c": "d"}})
        assert result == {"a": [1, 2], "b": {"c": "d"}}

    def test_coerces_unknown_objects_to_str(self) -> None:
        class Custom:
            def __str__(self) -> str:
                return "<custom>"

        assert _stringify_for_model(Custom()) == "<custom>"
        assert _stringify_for_model({"x": Custom()}) == {"x": "<custom>"}


class TestToolResultToFunctionResponse:
    def test_success_payload(self, provider: GeminiProvider) -> None:
        result = ToolResult(
            status=ToolStatus.SUCCESS,
            data={"output": "hello"},
            message="ran ok",
        )
        payload = provider._tool_result_to_function_response(result, "demo")
        assert payload["status"] == "success"
        assert payload["result"] == {"output": "hello"}
        assert payload["message"] == "ran ok"

    def test_error_payload_uses_metadata_code(self, provider: GeminiProvider) -> None:
        result = ToolResult(
            status=ToolStatus.ERROR,
            message="boom",
            metadata={"error_code": "CUSTOM"},
        )
        payload = provider._tool_result_to_function_response(result, "demo")
        assert payload == {
            "status": "error",
            "error": "boom",
            "error_code": "CUSTOM",
        }

    def test_error_payload_defaults_code(self, provider: GeminiProvider) -> None:
        result = ToolResult(status=ToolStatus.ERROR, message="boom")
        payload = provider._tool_result_to_function_response(result, "demo")
        assert payload["error_code"] == "EXECUTION_ERROR"


class TestExtractFunctionCalls:
    def test_collects_calls_in_order(self) -> None:
        response = _make_response(
            _make_function_call_part("a", {"x": 1}),
            _make_text_part("ignored"),
            _make_function_call_part("b", {"y": 2}),
        )
        calls = GeminiProvider._extract_function_calls(response)
        assert calls == [
            {"name": "a", "args": {"x": 1}},
            {"name": "b", "args": {"y": 2}},
        ]

    def test_no_candidates_returns_empty(self) -> None:
        assert GeminiProvider._extract_function_calls(SimpleNamespace()) == []

    def test_skips_calls_without_name(self) -> None:
        response = _make_response(
            SimpleNamespace(function_call=SimpleNamespace(name="", args={}), text=None)
        )
        assert GeminiProvider._extract_function_calls(response) == []


class TestExtractResponseText:
    def test_concatenates_text_parts(self) -> None:
        response = _make_response(
            _make_text_part("Hello "),
            _make_function_call_part("noop", {}),
            _make_text_part("world"),
        )
        assert GeminiProvider._extract_response_text(response) == "Hello world"

    def test_function_call_only_response_returns_empty(self) -> None:
        response = _make_response(_make_function_call_part("noop", {}))
        assert GeminiProvider._extract_response_text(response) == ""


class _StubTool:
    """SyncTool-like double whose ``execute`` returns a preset ToolResult."""

    def __init__(self, name: str, result: ToolResult) -> None:
        self._name = name
        self._result = result
        self.received_args: dict[str, Any] | None = None
        self.received_cwd: str | None = None

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, context):
        self.received_args = dict(context.parsed_args)
        self.received_cwd = context.working_directory
        return self._result


class TestExecuteFunctionCall:
    @pytest.mark.asyncio
    async def test_unknown_function_returns_error_payload(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = MagicMock()
        registry.get.return_value = None
        registry._tools = {"bash_execute": object(), "read_file": object()}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        tool_result, payload = await provider.execute_function_call(
            "run_code", {"x": 1}, working_directory="/tmp"
        )

        assert tool_result.status == ToolStatus.ERROR
        assert payload["status"] == "error"
        assert payload["error_code"] == "FUNCTION_NOT_FOUND"
        # Available tool names are advertised back to the model so it can recover.
        assert "bash_execute" in payload["error"]
        assert "read_file" in payload["error"]

    @pytest.mark.asyncio
    async def test_successful_tool_runs_and_payload_is_serializable(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = _StubTool(
            "bash_execute",
            ToolResult(
                status=ToolStatus.SUCCESS,
                data={"stdout": "Hello"},
                message="ok",
            ),
        )
        registry = MagicMock()
        registry.get.return_value = tool
        registry._tools = {"bash_execute": tool}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        tool_result, payload = await provider.execute_function_call(
            "bash_execute", {"command": "echo Hello"}, working_directory="/work"
        )

        assert tool_result.is_success
        assert tool.received_args == {"command": "echo Hello"}
        assert tool.received_cwd == "/work"
        assert payload == {
            "status": "success",
            "result": {"stdout": "Hello"},
            "message": "ok",
        }

    @pytest.mark.asyncio
    async def test_tool_exception_is_caught_and_returned_as_error(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class ExplodingTool:
            name = "bash_execute"

            async def execute(self, context):
                raise RuntimeError("boom inside tool")

        registry = MagicMock()
        registry.get.return_value = ExplodingTool()
        registry._tools = {"bash_execute": ExplodingTool()}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        tool_result, payload = await provider.execute_function_call(
            "bash_execute", {"command": "true"}
        )

        assert tool_result.status == ToolStatus.ERROR
        assert "boom inside tool" in tool_result.message
        assert payload["status"] == "error"
        assert payload["error_code"] == "EXECUTION_EXCEPTION"


class TestChatWithToolsLoop:
    @pytest.mark.asyncio
    async def test_text_only_response_returns_immediately(
        self, provider: GeminiProvider
    ) -> None:
        chat = _FakeChat([_make_response(_make_text_part("hi"))])

        text, results = await provider._gemini_chat_with_tools(chat, "ping")

        assert text == "hi"
        assert results == []
        assert chat.sent == ["ping"]  # no function_response sent back

    @pytest.mark.asyncio
    async def test_executes_tool_and_completes_with_text(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = _StubTool(
            "bash_execute",
            ToolResult(status=ToolStatus.SUCCESS, data="OK", message="done"),
        )
        registry = MagicMock()
        registry.get.return_value = tool
        registry._tools = {"bash_execute": tool}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        chat = _FakeChat(
            [
                _make_response(
                    _make_function_call_part("bash_execute", {"command": "ls"})
                ),
                _make_response(_make_text_part("All done.")),
            ]
        )

        text, results = await provider._gemini_chat_with_tools(
            chat, "list files", working_directory="/work"
        )

        assert text == "All done."
        assert len(results) == 1
        assert results[0].is_success
        # Two send_message calls: original message + function_response parts.
        assert len(chat.sent) == 2
        assert chat.sent[0] == "list files"
        # Second call must be a list of function_response Parts
        assert isinstance(chat.sent[1], list)
        assert len(chat.sent[1]) == 1

    @pytest.mark.asyncio
    async def test_hallucinated_tool_name_recovers_via_function_response(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces the original 'run_code' bug.

        With AFC the SDK raised KeyError; with the manual loop the unknown
        name becomes an error function_response and the model can respond.
        """
        registry = MagicMock()
        registry.get.return_value = None
        registry._tools = {"bash_execute": object()}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        chat = _FakeChat(
            [
                _make_response(_make_function_call_part("run_code", {})),
                _make_response(
                    _make_text_part("Sorry, I cannot run code directly.")
                ),
            ]
        )

        text, results = await provider._gemini_chat_with_tools(chat, "run my script")

        assert "Sorry" in text
        assert len(results) == 1
        assert results[0].status == ToolStatus.ERROR
        assert results[0].metadata["error_code"] == "FUNCTION_NOT_FOUND"
        # Crucially, no exception escaped — the chat survived the bad call.
        assert len(chat.sent) == 2

    @pytest.mark.asyncio
    async def test_max_iterations_is_enforced(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = _StubTool(
            "bash_execute",
            ToolResult(status=ToolStatus.SUCCESS, data="x", message="ok"),
        )
        registry = MagicMock()
        registry.get.return_value = tool
        registry._tools = {"bash_execute": tool}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        # Always returns a function_call → would loop forever without a cap.
        # Use a unique `command` per iteration so the per-turn ToolLoopGuard
        # (which catches identical-call repetition by default) does NOT fire
        # — that would mask the iteration-cap enforcement we want to test.
        chat = _FakeChat([
            _make_response(
                _make_function_call_part("bash_execute", {"command": f"x{i}"})
            )
            for i in range(20)
        ])

        text, results = await provider._gemini_chat_with_tools(
            chat, "loop please", max_iterations=3
        )

        # We executed exactly 3 tool calls before bailing out.
        successful = [r for r in results if r.is_success]
        assert len(successful) == 3
        # And we appended a synthetic MAX_ITERATIONS_EXCEEDED ToolResult.
        assert any(
            r.metadata.get("error_code") == "MAX_ITERATIONS_EXCEEDED"
            for r in results
            if r.metadata
        )

    @pytest.mark.asyncio
    async def test_parallel_function_calls_in_one_response(
        self, provider: GeminiProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = _StubTool(
            "bash_execute",
            ToolResult(status=ToolStatus.SUCCESS, data="ok", message=""),
        )
        registry = MagicMock()
        registry.get.return_value = tool
        registry._tools = {"bash_execute": tool}
        monkeypatch.setattr(
            "deile.tools.registry.get_tool_registry", lambda: registry
        )

        chat = _FakeChat(
            [
                _make_response(
                    _make_function_call_part("bash_execute", {"command": "a"}),
                    _make_function_call_part("bash_execute", {"command": "b"}),
                ),
                _make_response(_make_text_part("Done.")),
            ]
        )

        text, results = await provider._gemini_chat_with_tools(chat, "two cmds")

        assert text == "Done."
        assert len(results) == 2
        # Function responses must come back in a single batch (one send_message).
        assert isinstance(chat.sent[1], list)
        assert len(chat.sent[1]) == 2
