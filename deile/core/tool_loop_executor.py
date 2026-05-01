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

from deile.core.models.base import ModelMessage, ModelProvider
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.tools.base import ToolContext, ToolResult, ToolStatus
from deile.tools.registry import ToolRegistry, get_tool_registry

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 25
_SUMMARY_MAX_CHARS = 200


def _summarize(result: ToolResult, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """Build a short, single-line preview suitable for inline UI rendering."""
    if result.status == ToolStatus.ERROR:
        prefix = "error: "
        body = result.message or (str(result.error) if result.error else "(no message)")
    else:
        prefix = ""
        if result.data is not None:
            body = str(result.data)
        else:
            body = result.message or "ok"
    body = body.replace("\n", " ").replace("\r", " ").strip()
    text = prefix + body
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


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
    ) -> None:
        self._tool_registry = tool_registry or get_tool_registry()
        self._max_iterations = max_iterations
        self._event_publisher = event_publisher  # callable: (kind, name, **kw) -> awaitable

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
        provider_label = (
            getattr(provider, "model_name", None)
            or getattr(provider, "provider_id", None)
            or "model"
        )

        for iteration in range(self._max_iterations):
            pending_tool_calls: List[Tuple[str, str, Dict[str, Any]]] = []
            text_so_far_parts: List[str] = []
            error_seen = False
            # Captured from TOOL_USE_END events — providers that use reasoning/thinking
            # mode (e.g. DeepSeek-R1) require this to be echoed verbatim in the next
            # API call's assistant message, otherwise they return HTTP 400.
            last_reasoning_content: Optional[str] = None

            # Round-trip latency before the model starts streaming the next
            # iteration is otherwise silent — surface it as a STAGE so the UI
            # can keep the user informed instead of going blank.
            if iteration == 0:
                yield UnifiedStreamEvent(
                    type=StreamEventType.STAGE,
                    stage=f"Awaiting first token from {provider_label}",
                    iteration=iteration,
                )
            else:
                yield UnifiedStreamEvent(
                    type=StreamEventType.STAGE,
                    stage=f"Awaiting next response from {provider_label}",
                    iteration=iteration,
                )

            stream_iter = provider.generate_stream(
                history,
                system_instruction=system_instruction,
                tools=tools,
            )
            async for event in stream_iter:
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

            if error_seen:
                # Provider emitted ERROR — abort the loop and let the consumer
                # decide how to surface it. Aggregator path will translate to
                # AgentResponse(status=ERROR).
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
                yield UnifiedStreamEvent(
                    type=StreamEventType.STAGE,
                    stage=f"Executing {tc_name}",
                    iteration=iteration,
                )
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

                try:
                    result = await self._tool_registry.execute_tool(tc_name, ctx)
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
                await self._publish(
                    "completed" if result.is_success else "failed",
                    tc_name,
                    tc_id=tc_id,
                    success=result.is_success,
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
