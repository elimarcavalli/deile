"""OpenAI (and OpenAI-compatible) provider — multi-provider router implementation."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import openai

from deile.core.loop_guard import (check_tool_call, make_guard,
                                   record_tool_outcome)
from deile.core.models.base import (DEFAULT_MAX_OUTPUT_TOKENS,
                                    DEFAULT_MAX_TOOL_ITERATIONS, ModelMessage,
                                    ModelProvider, ModelResponse, ModelSize,
                                    ModelType, ModelUsage)
from deile.core.models.catalog import ModelHandle
from deile.core.models.error_mapping import make_envelope_builder
from deile.core.models.errors import ProviderInvocationError
from deile.core.models.provider_config import ProviderConfig
from deile.core.models.stream_events import (ModelUsageSnapshot,
                                             StreamEventType,
                                             UnifiedStreamEvent)
from deile.core.models.tool_execution import (OUTCOME_EXCEPTION,
                                              OUTCOME_NOT_FOUND,
                                              build_tool_result_payload,
                                              payload_to_text,
                                              resolve_and_execute_tool)

logger = logging.getLogger(__name__)


def _openai_body_fields(body: Dict[str, Any], exc: Exception) -> Tuple[str, str]:
    """Extract ``(err_code, err_msg)`` from an OpenAI error body.

    OpenAI nests the error under an ``error`` object carrying ``code`` and
    ``message``. A missing message falls back to ``str(exc)`` (matching the
    previous OpenAI-specific behavior).
    """
    err_dict: Any = body.get("error", {}) or {}
    if not isinstance(err_dict, dict):
        err_dict = {}
    return (
        str(err_dict.get("code", "") or ""),
        str(err_dict.get("message", "") or str(exc)),
    )


# OpenAI follows the standard HTTP-error shape; the only provider-specific
# knob is the body field layout. Exceptions without an HTTP body classify as
# ``unknown``.
_make_envelope = make_envelope_builder(_openai_body_fields)


class OpenAIProvider(ModelProvider):
    """ModelProvider implementation for OpenAI (and OpenAI-compatible) models."""

    def __init__(
        self,
        model_handle: ModelHandle,
        provider_config: ProviderConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_handle.model_id, **kwargs)
        self._handle = model_handle
        self._provider_config = provider_config

        api_key = self._require_api_key(provider_config, "OpenAIProvider")

        sdk_kwargs: Dict[str, Any] = dict(provider_config.sdk_kwargs or {})
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=provider_config.base_url,
            **sdk_kwargs,
        )

    # ------------------------------------------------------------------
    # ModelProvider contract
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def provider_id(self) -> str:
        return "openai"

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT, ModelType.CODE, ModelType.VISION]

    @property
    def model_size(self) -> ModelSize:
        return ModelSize.LARGE

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai_messages(
        messages: List[ModelMessage],
        system_instruction: Optional[str],
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        if system_instruction:
            result.append({"role": "system", "content": system_instruction})
        for m in messages:
            # Tool-result envelope produced by format_tool_result_message:
            # OpenAI's chat protocol uses role="tool" with a tool_call_id field.
            if m.metadata and m.metadata.get("_openai_tool_result"):
                tr_meta = m.metadata["_openai_tool_result"]
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr_meta["tool_call_id"],
                        "content": tr_meta["content"],
                    }
                )
                continue
            # Assistant turn that carried tool_calls (round-trip on next request)
            if m.metadata and m.metadata.get("_openai_tool_calls"):
                tc_blocks = m.metadata["_openai_tool_calls"]
                msg_dict: Dict[str, Any] = {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": tc_blocks,
                }
                rc = m.metadata.get("reasoning_content")
                if rc:
                    msg_dict["reasoning_content"] = rc
                result.append(msg_dict)
                continue
            msg_dict = {"role": m.role, "content": m.content}
            # Restore reasoning_content for providers that require it (e.g. DeepSeek)
            rc = m.metadata.get("reasoning_content") if m.metadata else None
            if rc and m.role == "assistant":
                msg_dict["reasoning_content"] = rc
            result.append(msg_dict)
        return result

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
        oai_msgs = self._to_openai_messages(messages, system_instruction)

        try:
            response = await self._client.chat.completions.create(
                model=self.model_name,
                messages=oai_msgs,
                max_completion_tokens=kwargs.pop("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                **kwargs,
            )
        except openai.APIError as exc:
            raise ProviderInvocationError(_make_envelope(exc, self.provider_id, self.model_name)) from exc

        text = response.choices[0].message.content or ""
        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        cached = self._extract_cached_tokens(response)
        usage = ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached,
            request_time=time.time() - start,
        )
        usage.cost_estimate = self.estimate_cost(usage)
        self._update_stats(usage)
        return ModelResponse(
            content=text,
            model_name=self.model_name,
            usage=usage,
            raw_response=response,
            finish_reason=response.choices[0].finish_reason,
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
        oai_msgs: List[Dict[str, Any]] = self._to_openai_messages(messages, system_instruction)
        oai_tools = [t.to_openai_function() for t in tools] if tools else []

        total_prompt = total_completion = total_cached = 0
        tool_results: List[Any] = []
        final_text = ""
        last_reasoning_content: Optional[str] = None
        guard = make_guard(session_id=str(kwargs.get("session_id", "")) or None)
        loop_aborted = False

        for _ in range(DEFAULT_MAX_TOOL_ITERATIONS):
            create_kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "messages": oai_msgs,
                "max_completion_tokens": kwargs.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            }
            if oai_tools:
                create_kwargs["tools"] = oai_tools
                create_kwargs["tool_choice"] = "auto"

            try:
                response = await self._client.chat.completions.create(**create_kwargs)
            except openai.APIError as exc:
                env = _make_envelope(exc, self.provider_id, self.model_name)
                await self._record_failed_usage(
                    session_id=kwargs.get("session_id", "default"),
                    start_time=start,
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                    cached_tokens=total_cached,
                    error_envelope=env,
                )
                raise ProviderInvocationError(env) from exc

            if response.usage:
                total_prompt += response.usage.prompt_tokens
                total_completion += response.usage.completion_tokens
                total_cached += self._extract_cached_tokens(response)

            msg = response.choices[0].message
            if msg.content:
                final_text += msg.content
            last_reasoning_content = getattr(msg, "reasoning_content", None)

            finish_reason = response.choices[0].finish_reason
            if finish_reason != "tool_calls" or not msg.tool_calls:
                break

            # Append assistant turn
            assistant_turn: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
            # DeepSeek reasoning models require reasoning_content echoed back verbatim.
            if last_reasoning_content:
                assistant_turn["reasoning_content"] = last_reasoning_content
            oai_msgs.append(assistant_turn)

            # Execute each tool call
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                # Loop guard — same defensive logic as Anthropic. See
                # deile.core.loop_guard for the detection rules.
                brk = check_tool_call(guard, tc.function.name, args)
                if brk is not None:
                    tool_results.append(brk.tool_result)
                    oai_msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(brk.payload),
                        }
                    )
                    final_text = (
                        (final_text + "\n\n" if final_text else "")
                        + brk.message
                    )
                    loop_aborted = True
                    continue
                tr, payload = await self._execute_tool(tc.function.name, args)
                tool_results.append(tr)
                record_tool_outcome(guard, tr)
                oai_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(payload),
                    }
                )
            if loop_aborted:
                break
        else:
            logger.warning("OpenAIProvider: tool loop hit max_iterations=%d", DEFAULT_MAX_TOOL_ITERATIONS)

        usage = ModelUsage(
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            total_tokens=total_prompt + total_completion,
            cached_tokens=total_cached,
            request_time=time.time() - start,
        )
        if last_reasoning_content:
            usage.extra["reasoning_content"] = last_reasoning_content
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
        oai_msgs = self._to_openai_messages(messages, system_instruction)

        create_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": oai_msgs,
            "max_completion_tokens": kwargs.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            create_kwargs["tools"] = [t.to_openai_function() for t in tools]
            create_kwargs["tool_choice"] = "auto"

        # tool_calls deltas come fragmented; index → accumulator
        pending_tool_calls: Dict[int, Dict[str, Any]] = {}
        final_usage: Optional[Any] = None
        final_cached: int = 0
        # DeepSeek reasoning models stream thinking tokens in delta.reasoning_content.
        # We accumulate the full reasoning text so it can be echoed verbatim in the
        # next API call — omitting it causes a 400 "reasoning_content must be passed back".
        accumulated_reasoning: str = ""

        try:
            stream_iter = await self._client.chat.completions.create(**create_kwargs)
            async for chunk in stream_iter:
                if getattr(chunk, "usage", None):
                    final_usage = chunk.usage
                    final_cached = self._extract_cached_tokens(chunk)

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                finish_reason = getattr(choice, "finish_reason", None)

                if delta is not None:
                    if getattr(delta, "content", None):
                        yield UnifiedStreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=delta.content,
                        )

                    rc_delta = getattr(delta, "reasoning_content", None)
                    if rc_delta:
                        accumulated_reasoning += rc_delta

                    tcs = getattr(delta, "tool_calls", None) or []
                    for tc in tcs:
                        idx = getattr(tc, "index", 0)
                        entry = pending_tool_calls.setdefault(
                            idx, {"id": "", "name": "", "args_text": ""}
                        )
                        if getattr(tc, "id", None):
                            entry["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                entry["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                entry["args_text"] += fn.arguments

                if finish_reason == "tool_calls":
                    # Emit START then END back-to-back so both events land in the same
                    # Rich Live render tick (12 Hz). The UI only sees the END state
                    # (full args visible), never "● write_file() running…" with no args.
                    rc = accumulated_reasoning or None
                    for idx, entry in pending_tool_calls.items():
                        try:
                            parsed_args = (
                                json.loads(entry["args_text"]) if entry["args_text"] else {}
                            )
                        except json.JSONDecodeError:
                            parsed_args = {"_raw": entry["args_text"]}
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
                            reasoning_content=rc,
                        )
                    pending_tool_calls.clear()
                    accumulated_reasoning = ""

            if final_usage is not None:
                snap = ModelUsageSnapshot(
                    input_tokens=final_usage.prompt_tokens,
                    output_tokens=final_usage.completion_tokens,
                    cached_tokens=final_cached,
                    cost_usd=self.estimate_cost(
                        ModelUsage(
                            prompt_tokens=final_usage.prompt_tokens,
                            completion_tokens=final_usage.completion_tokens,
                            cached_tokens=final_cached,
                        )
                    ),
                    model=f"{self.provider_id}:{self.model_name}",
                )
                yield UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL,
                    usage=snap,
                    reasoning_content=accumulated_reasoning or None,
                )
        except openai.APIError as exc:
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
        """Encode the assistant turn that carries tool_calls. The OpenAI-compatible
        protocol expects a ``role=assistant`` message with a ``tool_calls`` array.

        ``reasoning_content`` must be included verbatim for providers that use
        reasoning/thinking mode (e.g. DeepSeek-R1); omitting it causes a 400 error
        on the next API call.
        """
        tc_blocks = [
            {
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": tc_name,
                    "arguments": json.dumps(tc_args, default=str),
                },
            }
            for tc_id, tc_name, tc_args in pending_tool_calls
        ]
        metadata: Dict[str, Any] = {"_openai_tool_calls": tc_blocks}
        if reasoning_content:
            metadata["reasoning_content"] = reasoning_content
        return ModelMessage(
            role="assistant",
            content=text_so_far,
            metadata=metadata,
        )

    def format_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        payload: Any,
    ) -> ModelMessage:
        """OpenAI-compatible: tool results are role=tool messages keyed by tool_call_id."""
        payload_text = payload_to_text(payload)
        return ModelMessage(
            role="tool",
            content=payload_text,
            metadata={
                "_openai_tool_result": {
                    "tool_call_id": tool_call_id,
                    "content": payload_text,
                }
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_cached_tokens(response: Any) -> int:
        try:
            value = response.usage.prompt_tokens_details.cached_tokens or 0
        except AttributeError:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def estimate_cost(self, usage: ModelUsage) -> float:
        """OpenAI/DeepSeek-aware cost.

        Both providers report ``prompt_tokens`` as the FULL input total — the
        cached subset is reported separately (``prompt_tokens_details.cached_tokens``
        for OpenAI; ``prompt_cache_hit_tokens`` for DeepSeek). The base
        ``estimate_cost`` charges ``prompt_tokens`` at the full rate AND
        ``cached_tokens`` at the cached rate, double-counting the cached
        portion. Here we treat ``prompt_tokens`` as a superset and only charge
        the non-cached remainder at the full rate.
        """
        p = self.pricing
        if p is None:
            return 0.0
        cached = usage.cached_tokens or 0
        non_cached_input = max(usage.prompt_tokens - cached, 0)
        input_cost = (non_cached_input / 1_000_000) * p.input_per_1m_usd
        output_cost = (usage.completion_tokens / 1_000_000) * p.output_per_1m_usd
        cached_cost = 0.0
        if cached and p.cached_input_per_1m_usd is not None:
            cached_cost = (cached / 1_000_000) * p.cached_input_per_1m_usd
        elif cached:
            # No cached price published → charge cached portion at full rate.
            cached_cost = (cached / 1_000_000) * p.input_per_1m_usd
        return round(input_cost + output_cost + cached_cost, 8)

    async def _execute_tool(
        self, name: str, args: Dict[str, Any]
    ) -> Tuple[Any, Dict[str, Any]]:
        """Run one tool via ToolRegistry; return (ToolResult, json-serialisable payload).

        The resolve/not-found/execute/exception-wrap step is shared with the
        other providers via :func:`resolve_and_execute_tool`; only the OpenAI
        payload shape (a ``role=tool`` message body) is built here.
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
            exception_metadata={"function_name": name},
        )

        if outcome in (OUTCOME_NOT_FOUND, OUTCOME_EXCEPTION):
            return result, build_tool_result_payload(result, outcome, name)

        # Stamp tool name on metadata (Gemini's path does the same).
        if result.metadata is None:
            result.metadata = {}
        result.metadata.setdefault("function_name", name)

        # Payload carries data (e.g. read_file body) AND message (e.g.
        # write_file's POST_WRITE_VALIDATION_REQUIRED hint).
        payload = build_tool_result_payload(
            result, outcome, name,
            include_message=True,
            include_data_on_error=True,
        )
        return result, payload
