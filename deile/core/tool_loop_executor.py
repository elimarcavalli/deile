"""Provider-agnostic tool-loop executor for streaming turns.

Single source of truth for the iterative function-calling loop. Each provider
exposes ``generate_stream(tools=...)`` (which only emits events — never
executes tools) plus the two shape adapters
``format_assistant_tool_use_message`` and ``format_tool_result_message``;
``ToolLoopExecutor`` orchestrates iteration, registry execution, and
``TOOL_RESULT`` emission.

This file is the deduplication target for the previous per-provider
``chat_with_tools`` copies.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from deile.core.loop_guard import (ToolLoopGuard, format_loop_break_message,
                                   make_guard, tool_result_made_progress)
from deile.core.models.base import ModelMessage, ModelProvider
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.tools.base import ToolContext, ToolResult, ToolStatus
from deile.tools.registry import ToolRegistry, get_tool_registry
from deile.ui.stage_cascade import cascade_stream, cascade_until
from deile.ui.stage_messages import get_stage_message  # noqa: F401

# Map tool names (as emitted by the model) to message-library scenario keys.
# When a tool is registered, use its specific scenario for richer feedback.
_TOOL_SCENARIO_MAP: Dict[str, str] = {
    "pip_install": "tool_pip_install",
    "run_tests": "tool_run_tests",
    "test_runner": "tool_run_tests",
    "find_in_files": "tool_find_files",
    "search_tool": "tool_find_files",
    "bash_execute": "tool_bash",
    "write_file": "tool_write_file",
    "file_write": "tool_write_file",
}

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 25
_SUMMARY_MAX_CHARS = 200


def _summarize(result: ToolResult, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """Build a short, single-line preview suitable for inline UI rendering.

    Tool-name aware: for known tools, render a semantic summary
    (``exit 0 • 23ms``, ``46 bytes • 2 lines``, ``3 entries: a, b, c``)
    instead of dumping ``str(result.data)`` which would surface Python repr
    of dicts/lists in the terminal.
    """
    if result.status == ToolStatus.ERROR:
        prefix = "error: "
        body = result.message or (str(result.error) if result.error else "(no message)")
        body = body.replace("\n", " ").replace("\r", " ").strip()
        text = prefix + body
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    # Success path — try a tool-specific renderer first.
    meta = result.metadata or {}
    tool_name = str(meta.get("function_name") or "")
    semantic = _semantic_summary(tool_name, result)
    if semantic is not None:
        body = semantic
    elif result.data is not None:
        body = str(result.data)
    else:
        body = result.message or "ok"
    body = body.replace("\n", " ").replace("\r", " ").strip()
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return body


def _semantic_summary(tool_name: str, result: ToolResult) -> Optional[str]:
    """Tool-specific one-line summaries — return ``None`` to fall back.

    Strict on shape: we read from ``metadata``/``data`` defensively so
    odd providers can't crash the renderer. Anything weird → return None
    and let the generic path handle it.
    """
    meta = result.metadata or {}
    data = result.data

    if tool_name == "bash_execute" or tool_name == "python_execute":
        # bash_tool.py packs data as dict; execution_tools.py packs string in data + dict in metadata.
        exit_code = None
        exec_time = None
        stdout = ""
        stderr = ""
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            exec_time = data.get("execution_time")
            stdout = str(data.get("stdout") or "")
            stderr = str(data.get("stderr") or "")
        else:
            exit_code = meta.get("exit_code")
            exec_time = meta.get("execution_time")
            stdout = str(meta.get("stdout") or (data if isinstance(data, str) else ""))
            stderr = str(meta.get("stderr") or "")
        parts = []
        if exit_code is not None:
            parts.append(f"exit {exit_code}")
        if isinstance(exec_time, (int, float)):
            parts.append(f"{int(exec_time * 1000)}ms")
        head = " • ".join(parts) or "ok"
        # Append first line of stdout (or stderr if errored) for context.
        trailer = ""
        snippet_source = stderr if (isinstance(exit_code, int) and exit_code != 0 and stderr) else stdout
        first_line = next(
            (ln.strip() for ln in snippet_source.splitlines() if ln.strip()),
            "",
        )
        if first_line:
            if len(first_line) > 80:
                first_line = first_line[:77] + "…"
            trailer = f" • {first_line}"
        return head + trailer

    if tool_name == "read_file":
        size = meta.get("file_size")
        if size is None and isinstance(data, str):
            size = len(data)
        lines = None
        if isinstance(data, str):
            lines = data.count("\n") + (0 if data.endswith("\n") else 1) if data else 0
        if size is not None and lines is not None:
            return f"{size} bytes • {lines} line" + ("" if lines == 1 else "s")
        if size is not None:
            return f"{size} bytes"
        return None

    if tool_name == "write_file":
        length = meta.get("content_length")
        rel = meta.get("project_relative_path") or meta.get("input_path") or ""
        if isinstance(length, int) and rel:
            return f"{length} bytes written → {rel}"
        if isinstance(length, int):
            return f"{length} bytes written"
        return None

    if tool_name == "edit_file":
        rel = meta.get("project_relative_path") or meta.get("input_path") or ""
        patches = meta.get("patches_applied") or meta.get("patch_count")
        if isinstance(patches, int) and rel:
            return f"{patches} patch" + ("" if patches == 1 else "es") + f" → {rel}"
        if rel:
            return f"updated → {rel}"
        return None

    if tool_name == "list_files":
        total = meta.get("total_items")
        if total is None and isinstance(data, list):
            total = len(data)
        names: list = []
        if isinstance(data, list):
            for entry in data[:5]:
                if isinstance(entry, dict):
                    n = entry.get("name") or entry.get("path") or ""
                    if entry.get("type") == "directory":
                        n = f"{n}/"
                    if n:
                        names.append(str(n))
                elif isinstance(entry, str):
                    names.append(entry)
        if total is not None and names:
            preview = ", ".join(names[:3])
            if total > 3:
                preview += f", … +{total - 3}"
            return f"{total} entr{'y' if total == 1 else 'ies'}: {preview}"
        if total is not None:
            return f"{total} entr{'y' if total == 1 else 'ies'}"
        return None

    if tool_name == "delete_file":
        if result.message:
            msg = result.message.replace("Successfully deleted directory: ", "deleted dir ")
            msg = msg.replace("Successfully deleted file: ", "deleted ")
            msg = msg.replace("Successfully deleted: ", "deleted ")
            return msg
        return "deleted"

    return None


def _payload_for_model(result: ToolResult) -> Dict[str, Any]:
    """Encode a ToolResult into a JSON-serializable dict for the next round-trip."""
    if result.status == ToolStatus.ERROR:
        return {
            "status": "error",
            "error": result.message or (str(result.error) if result.error else "tool failed"),
        }
    payload: Dict[str, Any] = {"status": "success"}
    if result.data is not None:
        payload["result"] = str(result.data)
    if result.message:
        payload["message"] = result.message
    return payload


class ToolLoopExecutor:
    """Run a multi-iteration tool-use loop against any provider.

    The executor never decides *what* to call — the model does. It only:

    * Forwards every ``UnifiedStreamEvent`` from the provider to its consumer
      (UI, aggregator, anything).
    * Collects ``TOOL_USE_END`` events to know which tools the model wants to
      run after the round closes.
    * Executes those tools via the registry, emits ``TOOL_RESULT`` events,
      appends the result message to the rolling history, and re-invokes the
      provider for the next iteration.
    """

    def __init__(
        self,
        tool_registry: Optional[ToolRegistry] = None,
        max_iterations: int = MAX_TOOL_ITERATIONS,
        event_publisher: Optional[Any] = None,
        loop_guard: Optional[ToolLoopGuard] = None,
    ) -> None:
        self._tool_registry = tool_registry or get_tool_registry()
        self._max_iterations = max_iterations
        self._event_publisher = event_publisher  # callable: (kind, name, **kw) -> awaitable
        # Each ``run()`` invocation builds its own guard (one per turn). The
        # constructor accepts an explicit guard only so tests can inject a
        # pre-configured detector — production callers leave it as ``None``.
        self._loop_guard_override = loop_guard

    async def run(
        self,
        provider: ModelProvider,
        messages: List[ModelMessage],
        tools: List[Any],
        system_instruction: Optional[str] = None,
        working_directory: str = ".",
        session_data: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        """Stream the full tool-loop end-to-end.

        Yields every event the provider emits, plus ``TOOL_RESULT`` events
        produced by this executor for each tool the registry runs.
        """
        history = list(messages)
        _ = (
            getattr(provider, "model_name", None)
            or getattr(provider, "provider_id", None)
            or "model"
        )
        # Per-turn loop detector. Defensive against the model spinning on
        # the same call when its previous tool returned an error or empty
        # data — see deile.core.loop_guard for the detection rules.
        guard = self._loop_guard_override or make_guard(
            session_id=str((session_data or {}).get("session_id", ""))
            or None,
        )

        for iteration in range(self._max_iterations):
            pending_tool_calls: List[Tuple[str, str, Dict[str, Any]]] = []
            text_so_far_parts: List[str] = []
            error_seen = False
            last_error_envelope: Optional[Any] = None
            # Captured from TOOL_USE_END events — providers that use reasoning/thinking
            # mode (e.g. DeepSeek-R1) require this to be echoed verbatim in the next
            # API call's assistant message, otherwise they return HTTP 400.
            last_reasoning_content: Optional[str] = None

            # Round-trip latency before the model starts streaming the next
            # iteration is otherwise silent — surface it as a STAGE cascade
            # so the UI keeps evolving (3s → 10s → 30s) until the first event.
            stream_iter = provider.generate_stream(
                history,
                system_instruction=system_instruction,
                tools=tools,
            )
            cascade_key = "await_first_token" if iteration == 0 else "await_next_response"
            cascade_ctx: Dict[str, Any] = (
                {} if iteration == 0 else {"iteration": str(iteration + 1)}
            )
            async for event in cascade_stream(
                stream_iter,
                message_key=cascade_key,
                event_iteration=iteration,
                **cascade_ctx,
            ):
                event.iteration = iteration
                yield event

                if event.type is StreamEventType.TEXT_DELTA and event.text:
                    text_so_far_parts.append(event.text)
                elif event.type is StreamEventType.TOOL_USE_END:
                    pending_tool_calls.append(
                        (
                            event.tool_call_id or "",
                            event.tool_name or "",
                            event.arguments or {},
                        )
                    )
                    if event.reasoning_content:
                        last_reasoning_content = event.reasoning_content
                elif event.type is StreamEventType.ERROR:
                    error_seen = True
                    last_error_envelope = event.error_envelope

            if error_seen:
                # Provider emitted ERROR — emit a user-friendly message for
                # context_length_exceeded, then abort the loop.
                if (
                    last_error_envelope is not None
                    and getattr(last_error_envelope, "error_type", None) == "context_length_exceeded"
                ):
                    model_id = getattr(last_error_envelope, "model_id", "modelo")
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text=(
                            f"O histórico desta conversa excedeu o limite de contexto do modelo **{model_id}**.\n\n"
                            "**Como resolver:**\n"
                            "• `/clear` — limpa o histórico e inicia uma nova sessão\n"
                            "• `/model select` — escolha um modelo com janela de contexto maior\n"
                            "• Divida sua pergunta em partes menores e mais objetivas\n"
                        ),
                    )
                return

            if not pending_tool_calls:
                # Model produced its final answer — terminate cleanly.
                return

            # Persist the assistant turn that requested the tools.
            text_so_far = "".join(text_so_far_parts)
            history.append(
                provider.format_assistant_tool_use_message(
                    pending_tool_calls,
                    text_so_far=text_so_far,
                    reasoning_content=last_reasoning_content,
                )
            )

            # Execute each tool sequentially, emitting TOOL_RESULT events.
            for tc_id, tc_name, tc_args in pending_tool_calls:
                # Use a specific scenario key when available for richer feedback.
                tool_key = _TOOL_SCENARIO_MAP.get(tc_name, "tool_executing")
                tool_kwargs: Dict[str, Any] = {"tool": tc_name}
                if tool_key == "tool_pip_install":
                    tool_kwargs["package"] = (
                        str(list(tc_args.values())[0]) if tc_args else tc_name
                    )
                elif tool_key == "tool_bash":
                    raw_cmd = ""
                    if tc_args:
                        raw_cmd = str(
                            tc_args.get("command")
                            or tc_args.get("cmd")
                            or tc_args.get("script")
                            or list(tc_args.values())[0]
                        )
                    tool_kwargs["cmd"] = raw_cmd[:60] or tc_name
                elif tool_key == "tool_write_file":
                    tool_kwargs["file"] = str(
                        tc_args.get("path") or tc_args.get("file_path") or tc_name
                    ) if tc_args else tc_name
                elif tool_key == "tool_find_files":
                    tool_kwargs.setdefault(
                        "path",
                        str(tc_args.get("path") or tc_args.get("directory") or "workspace")
                        if tc_args
                        else "workspace",
                    )
                    tool_kwargs.setdefault("matches", 0)
                    tool_kwargs.setdefault("scanned", 0)
                elif tool_key == "tool_run_tests":
                    tool_kwargs["target"] = (
                        str(tc_args.get("target") or tc_args.get("path") or tc_name)
                        if tc_args
                        else tc_name
                    )
                    tool_kwargs.setdefault("count", 0)

                # ── Loop guard: detect identical-call / windowed / no-progress
                # spirals before we burn another round-trip. If the guard
                # trips, emit a synthetic TOOL_RESULT (so the UI sees what
                # happened) plus a TEXT_DELTA explaining the abort, then
                # stop the entire run — the model has been demonstrably
                # going in circles.
                abort = guard.check(tc_name, tc_args)
                if abort is not None:
                    summary = format_loop_break_message(abort)
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                        tool_status="error",
                        tool_result_summary=summary[:200],
                        tool_result_data=None,
                        tool_metadata={
                            "function_name": tc_name,
                            "tool_call_id": tc_id,
                            "loop_break": True,
                            "loop_break_kind": abort.kind.value,
                            "loop_break_args_hash": abort.args_hash,
                        },
                        iteration=iteration,
                    )
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text=abort.user_message(),
                        source="loop_guard",
                    )
                    await self._publish(
                        "failed",
                        tc_name,
                        tc_id=tc_id,
                        error=summary,
                        loop_break=True,
                    )
                    return

                await self._publish("invoked", tc_name, tc_id=tc_id, args=tc_args)

                ctx = ToolContext(
                    user_input="",
                    parsed_args=dict(tc_args or {}),
                    session_data=dict(session_data or {}),
                    working_directory=working_directory or ".",
                    file_list=[],
                    metadata={
                        "execution_method": "tool_loop_executor",
                        "function_name": tc_name,
                        "tool_call_id": tc_id,
                    },
                )

                # Run the tool under a temporal cascade so the user sees the
                # spinner text evolve when the tool takes >3s, >10s, >30s.
                # cascade_until yields STAGE events while the awaitable runs
                # and a final ("result", value) tuple when it completes; it
                # re-raises the underlying exception if the tool fails.
                result: Any = None
                try:
                    async for item in cascade_until(
                        self._tool_registry.execute_tool(tc_name, ctx),
                        message_key=tool_key,
                        event_iteration=iteration,
                        **tool_kwargs,
                    ):
                        if isinstance(item, tuple) and item and item[0] == "result":
                            result = item[1]
                        elif isinstance(item, UnifiedStreamEvent):
                            yield item
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error(
                        "Tool '%s' raised in ToolLoopExecutor: %s", tc_name, exc, exc_info=True
                    )
                    err_result = ToolResult(
                        status=ToolStatus.ERROR,
                        message=f"{type(exc).__name__}: {exc}",
                        error=exc,
                        metadata={"function_name": tc_name, "tool_call_id": tc_id},
                    )
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                        tool_status="error",
                        tool_result_summary=_summarize(err_result),
                        tool_result_data=None,
                        tool_metadata=dict(err_result.metadata or {}),
                        iteration=iteration,
                    )
                    history.append(
                        provider.format_tool_result_message(
                            tc_id,
                            tc_name,
                            _payload_for_model(err_result),
                        )
                    )
                    # An exception escaping the registry is always "no progress"
                    # — feed that into the guard so a string of failures
                    # eventually trips the no-progress rule.
                    guard.record_result(made_progress=False)
                    await self._publish("failed", tc_name, tc_id=tc_id, error=exc)
                    continue

                if result.metadata is None:
                    result.metadata = {}
                result.metadata.setdefault("function_name", tc_name)
                result.metadata.setdefault("tool_call_id", tc_id)

                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_RESULT,
                    tool_call_id=tc_id,
                    tool_name=tc_name,
                    tool_status="success" if result.is_success else "error",
                    tool_result_summary=_summarize(result),
                    tool_result_data=result.data,
                    tool_metadata=dict(result.metadata or {}),
                    iteration=iteration,
                )
                history.append(
                    provider.format_tool_result_message(
                        tc_id, tc_name, _payload_for_model(result)
                    )
                )
                # Feed the result into the guard so the no-progress rule can
                # observe consecutive empty/error returns.
                guard.record_result(
                    made_progress=tool_result_made_progress(result)
                )
                await self._publish(
                    "completed" if result.is_success else "failed",
                    tc_name,
                    tc_id=tc_id,
                    success=result.is_success,
                )

        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message("max_iterations", "initial", max=self._max_iterations),
        )
        logger.warning(
            "ToolLoopExecutor: hit max_iterations=%d for provider=%s",
            self._max_iterations,
            getattr(provider, "provider_id", "?"),
        )

    async def _publish(self, kind: str, tool_name: str, **kw: Any) -> None:
        """Best-effort fanout to the event publisher; never raises."""
        if self._event_publisher is None:
            return
        try:
            await self._event_publisher(kind, tool_name, **kw)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("event_publisher failed (kind=%s, tool=%s): %s", kind, tool_name, exc)
