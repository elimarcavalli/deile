"""Anthropic (Claude) provider — multi-provider router implementation."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import anthropic

from deile.core.loop_guard import (format_loop_break_message, make_guard,
                                   make_loop_break_result,
                                   tool_result_made_progress)
from deile.core.models.base import (ModelMessage, ModelProvider, ModelResponse,
                                    ModelSize, ModelType, ModelUsage)
from deile.core.models.catalog import ModelHandle
from deile.core.models.error_mapping import make_envelope_builder
from deile.core.models.errors import ProviderInvocationError
from deile.core.models.provider_config import ProviderConfig
from deile.core.models.stream_events import (ModelUsageSnapshot,
                                             StreamEventType,
                                             UnifiedStreamEvent)
from deile.core.models.tool_execution import (OUTCOME_EXCEPTION,
                                              OUTCOME_NOT_FOUND,
                                              payload_to_text,
                                              resolve_and_execute_tool)

logger = logging.getLogger(__name__)

_MAX_TOOL_ITERATIONS = 25
_DEFAULT_MAX_TOKENS = 16384


def _anthropic_body_fields(body: Dict[str, Any], exc: Exception) -> Tuple[str, str]:
    """Extract ``(err_code, err_msg)`` from an Anthropic error body.

    Anthropic nests the error code under ``type`` and the message under
    ``message`` at the top level of the body. A missing message stays empty
    (matching the previous Anthropic-specific behavior).
    """
    del exc  # Anthropic does not fall back to str(exc) for a dict body.
    return str(body.get("type", "") or ""), str(body.get("message", "") or "")


# Anthropic follows the standard HTTP-error shape; the only provider-specific
# knobs are the body field layout and the "prompt is too long" marker.
_make_envelope = make_envelope_builder(
    _anthropic_body_fields, extra_msg_markers=("prompt is too long",)
)


class AnthropicProvider(ModelProvider):
    """ModelProvider implementation for Anthropic (Claude) models."""

    def __init__(
        self,
        model_handle: ModelHandle,
        provider_config: ProviderConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_handle.model_id, **kwargs)
        self._handle = model_handle
        self._provider_config = provider_config

        api_key = self._require_api_key(provider_config, "AnthropicProvider")

        sdk_kwargs: Dict[str, Any] = dict(provider_config.sdk_kwargs or {})
        # default_headers from YAML (e.g. anthropic-beta: prompt-caching-2024-07-31)
        default_headers = sdk_kwargs.pop("default_headers", None)
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            default_headers=default_headers or {},
            **sdk_kwargs,
        )

    # ------------------------------------------------------------------
    # ModelProvider contract
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def provider_id(self) -> str:
        return "anthropic"

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT, ModelType.CODE, ModelType.VISION]

    @property
    def model_size(self) -> ModelSize:
        return ModelSize.LARGE

    # ------------------------------------------------------------------
    # Message conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_anthropic_messages(
        messages: List[ModelMessage],
    ) -> List[Dict[str, Any]]:
        """Convert ModelMessage list → Anthropic messages array (no system).

        When a message carries ``metadata['_anthropic_content_blocks']``, those
        structured blocks (e.g. ``tool_use`` / ``tool_result``) are sent
        verbatim instead of the plain text — required by Anthropic's tool-use
        protocol so the next turn round-trips correctly.
        """
        result = []
        for m in messages:
            if m.role == "system":
                continue
            blocks = m.metadata.get("_anthropic_content_blocks") if m.metadata else None
            if blocks:
                result.append({"role": m.role, "content": blocks})
            else:
                result.append({"role": m.role, "content": m.content})
        return result

    @staticmethod
    def _system_blocks(system: str) -> List[Dict[str, Any]]:
        """Wrap the system prompt in Anthropic's cached text-block array.

        The ephemeral ``cache_control`` marker lets Anthropic reuse the system
        prompt across requests. Centralized here so ``generate``,
        ``chat_with_tools`` and ``generate_stream`` cannot drift apart.
        """
        return [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

    # ------------------------------------------------------------------
    # generate()
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        start = time.time()
        system_instruction = self._compose_system_instruction(system_instruction)
        system = self._extract_system(messages, system_instruction)
        anthropic_msgs = self._to_anthropic_messages(messages)

        create_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": kwargs.pop("max_tokens", _DEFAULT_MAX_TOKENS),
            "messages": anthropic_msgs,
        }
        if system:
            create_kwargs["system"] = self._system_blocks(system)

        try:
            response = await self._client.messages.create(**create_kwargs, **kwargs)
        except anthropic.APIError as exc:
            raise ProviderInvocationError(_make_envelope(exc, self.provider_id, self.model_name)) from exc

        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        usage = ModelUsage(
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            request_time=time.time() - start,
        )
        usage.cost_estimate = self.estimate_cost(usage)
        self._update_stats(usage)
        return ModelResponse(
            content=text,
            model_name=self.model_name,
            usage=usage,
            raw_response=response,
            finish_reason=response.stop_reason,
        )

    # ------------------------------------------------------------------
    # chat_with_tools()
    # ------------------------------------------------------------------

    # TODO(streaming-cleanup): once all callers migrate to ToolLoopExecutor + generate_stream(tools=...), this method can be removed. Currently still used by deile/core/agent.py:_process_iterative_function_calling.
    async def chat_with_tools(
        self,
        messages: List[ModelMessage],
        tools: List[Any],  # List[ToolSchema]
        system_instruction: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Any], ModelUsage]:

        start = time.time()
        system_instruction = self._compose_system_instruction(system_instruction)
        system = self._extract_system(messages, system_instruction)
        anthropic_msgs: List[Dict[str, Any]] = self._to_anthropic_messages(messages)
        anthropic_tools = [t.to_anthropic_tool() for t in tools] if tools else []

        total_input = total_output = total_cached = 0
        tool_results: List[Any] = []
        final_text = ""
        guard = make_guard(session_id=str(kwargs.get("session_id", "")) or None)
        loop_aborted = False

        for iteration in range(_MAX_TOOL_ITERATIONS):
            create_kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "max_tokens": kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
                "messages": anthropic_msgs,
            }
            if system:
                create_kwargs["system"] = self._system_blocks(system)
            if anthropic_tools:
                create_kwargs["tools"] = anthropic_tools

            try:
                response = await self._client.messages.create(**create_kwargs)
            except anthropic.APIError as exc:
                env = _make_envelope(exc, self.provider_id, self.model_name)
                await self._record_failed_usage(
                    session_id=kwargs.get("session_id", "default"),
                    start_time=start,
                    prompt_tokens=total_input,
                    completion_tokens=total_output,
                    cached_tokens=total_cached,
                    error_envelope=env,
                )
                raise ProviderInvocationError(env) from exc

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens
            total_cached += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            total_cached += getattr(response.usage, "cache_creation_input_tokens", 0) or 0

            # Accumulate text blocks (only type=="text" blocks carry text)
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    final_text += getattr(block, "text", "")

            if response.stop_reason != "tool_use":
                break

            # Process tool_use blocks
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                break

            # Append assistant turn
            anthropic_msgs.append({"role": "assistant", "content": response.content})

            # Execute each tool call and build tool_result turn
            tool_result_content = []
            for block in tool_use_blocks:
                # Loop guard — see deile.core.loop_guard. We check BEFORE the
                # tool runs; if the guard trips, replace the would-be tool
                # invocation with a synthetic error result so the model can
                # see we refused, append the abort text to final_text, and
                # break out of the entire iteration loop.
                abort = guard.check(block.name, dict(block.input or {}))
                if abort is not None:
                    tr, payload = make_loop_break_result(abort)
                    tool_results.append(tr)
                    tool_result_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                        }
                    )
                    final_text = (
                        (final_text + "\n\n" if final_text else "")
                        + format_loop_break_message(abort)
                    )
                    loop_aborted = True
                    continue
                tr, payload = await self._execute_tool(block.name, block.input)
                tool_results.append(tr)
                guard.record_result(made_progress=tool_result_made_progress(tr))
                tool_result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                    }
                )

            anthropic_msgs.append({"role": "user", "content": tool_result_content})
            if loop_aborted:
                break
        else:
            logger.warning("AnthropicProvider: tool loop hit max_iterations=%d", _MAX_TOOL_ITERATIONS)

        usage = ModelUsage(
            prompt_tokens=total_input,
            completion_tokens=total_output,
            total_tokens=total_input + total_output,
            cached_tokens=total_cached,
            request_time=time.time() - start,
        )
        usage.cost_estimate = self.estimate_cost(usage)
        self._update_stats(usage)
        latency_ms = int((time.time() - start) * 1000)
        await self._record_usage(
            session_id=kwargs.get("session_id", "default"),
            usage=usage,
            latency_ms=latency_ms,
            success=True,
        )
        return final_text.strip(), tool_results, usage

    # ------------------------------------------------------------------
    # generate_stream()
    # ------------------------------------------------------------------

    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        system_instruction = self._compose_system_instruction(system_instruction)
        system = self._extract_system(messages, system_instruction)
        anthropic_msgs = self._to_anthropic_messages(messages)

        create_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
            "messages": anthropic_msgs,
        }
        if system:
            create_kwargs["system"] = self._system_blocks(system)
        if tools:
            create_kwargs["tools"] = [t.to_anthropic_tool() for t in tools]

        # Per-tool-use accumulator: index → (id, name, json_args_text)
        pending_tool_uses: Dict[int, Dict[str, Any]] = {}

        try:
            async with self._client.messages.stream(**create_kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)

                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if block is not None and getattr(block, "type", None) == "tool_use":
                            idx = getattr(event, "index", 0)
                            pending_tool_uses[idx] = {
                                "id": getattr(block, "id", ""),
                                "name": getattr(block, "name", ""),
                                "args_text": "",
                            }

                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            yield UnifiedStreamEvent(
                                type=StreamEventType.TEXT_DELTA,
                                text=delta.text,
                            )
                        elif dtype == "input_json_delta":
                            idx = getattr(event, "index", 0)
                            partial = getattr(delta, "partial_json", "") or ""
                            entry = pending_tool_uses.get(idx)
                            if entry is not None:
                                entry["args_text"] += partial

                    elif etype == "content_block_stop":
                        idx = getattr(event, "index", 0)
                        entry = pending_tool_uses.pop(idx, None)
                        if entry is not None:
                            try:
                                parsed_args = (
                                    json.loads(entry["args_text"]) if entry["args_text"] else {}
                                )
                            except json.JSONDecodeError:
                                parsed_args = {"_raw": entry["args_text"]}
                            # Emit START then END back-to-back: both arrive in the same
                            # Rich Live render tick so the block always shows args from
                            # the moment it appears (no "● write_file() running…" state).
                            yield UnifiedStreamEvent(
                                type=StreamEventType.TOOL_USE_START,
                                tool_call_id=entry["id"],
                                tool_name=entry["name"],
                            )
                            yield UnifiedStreamEvent(
                                type=StreamEventType.TOOL_USE_END,
                                tool_call_id=entry["id"],
                                tool_name=entry["name"],
                                arguments=parsed_args,
                            )

                    elif etype == "message_stop":
                        final = await stream.get_final_message()
                        usage = final.usage
                        cached = (
                            (getattr(usage, "cache_read_input_tokens", 0) or 0)
                            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                        )
                        snap = ModelUsageSnapshot(
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cached_tokens=cached,
                            cost_usd=self.estimate_cost(
                                ModelUsage(
                                    prompt_tokens=usage.input_tokens,
                                    completion_tokens=usage.output_tokens,
                                    cached_tokens=cached,
                                )
                            ),
                            model=f"{self.provider_id}:{self.model_name}",
                        )
                        yield UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL, usage=snap)
        except anthropic.APIError as exc:
            envelope = _make_envelope(exc, self.provider_id, self.model_name)
            yield UnifiedStreamEvent(type=StreamEventType.ERROR, error_envelope=envelope)

    # ------------------------------------------------------------------
    # Tool-loop adapters
    # ------------------------------------------------------------------

    def format_assistant_tool_use_message(
        self,
        pending_tool_calls: List[Tuple[str, str, Dict[str, Any]]],
        text_so_far: str = "",
        reasoning_content: Optional[str] = None,
    ) -> ModelMessage:
        """Anthropic requires the assistant turn to carry the tool_use blocks
        verbatim before the matching tool_result blocks arrive in the next user
        turn. We round-trip them as JSON-serializable dicts in metadata so the
        message-conversion layer can rehydrate the structure."""
        content_blocks: List[Dict[str, Any]] = []
        if text_so_far:
            content_blocks.append({"type": "text", "text": text_so_far})
        for tc_id, tc_name, tc_args in pending_tool_calls:
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc_id,
                    "name": tc_name,
                    "input": tc_args,
                }
            )
        return ModelMessage(
            role="assistant",
            content=text_so_far,
            metadata={"_anthropic_content_blocks": content_blocks},
        )

    def format_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        payload: Any,
    ) -> ModelMessage:
        """Anthropic encodes tool results as a user-turn tool_result block."""
        payload_text = payload_to_text(payload)

        block = {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": [{"type": "text", "text": payload_text}],
        }
        return ModelMessage(
            role="user",
            content=payload_text,
            metadata={"_anthropic_content_blocks": [block]},
        )

    # ------------------------------------------------------------------
    # Internal tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self, name: str, args: Dict[str, Any]
    ) -> Tuple[Any, Dict[str, Any]]:
        """Run one tool via ToolRegistry; return (ToolResult, json-serialisable payload).

        The resolve/not-found/execute/exception-wrap step is shared with the
        other providers via :func:`resolve_and_execute_tool`; only the
        Anthropic payload shape is built here.
        """
        from deile.tools.base import ToolContext

        result, outcome = await resolve_and_execute_tool(
            name=name,
            args=args,
            not_found_message_fn=lambda n, avail: (
                f"Tool '{n}' not found. Available: {', '.join(avail)}"
            ),
            context_factory=lambda _n, a, _t: ToolContext(
                user_input="", parsed_args=dict(a or {})
            ),
        )

        if outcome in (OUTCOME_NOT_FOUND, OUTCOME_EXCEPTION):
            payload = {"error": result.message, "status": "error"}
        elif result.is_success:
            payload = {
                "status": "success",
                "result": str(result.data) if result.data is not None else "",
            }
        else:
            payload = {"status": "error", "error": result.message or f"{name} failed"}
        return result, payload
