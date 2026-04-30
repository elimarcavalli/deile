"""OpenAI (and OpenAI-compatible) provider — multi-provider router implementation."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import openai

from deile.core.models.base import (
    ModelMessage,
    ModelProvider,
    ModelResponse,
    ModelSize,
    ModelType,
    ModelUsage,
)
from deile.core.models.catalog import ModelHandle, ModelPricing
from deile.core.models.errors import ProviderErrorEnvelope, ProviderInvocationError
from deile.core.models.provider_config import ProviderConfig
from deile.core.models.stream_events import (
    ModelUsageSnapshot,
    StreamEventType,
    UnifiedStreamEvent,
)
from deile.core.models.tier import ModelTier

logger = logging.getLogger(__name__)

_MAX_TOOL_ITERATIONS = 10
_DEFAULT_MAX_TOKENS = 8192


def _classify_openai_error(exc: openai.APIStatusError) -> str:
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status and 400 <= status < 500:
        return "invalid_request"
    if status and status >= 500:
        return "server"
    return "unknown"


def _make_envelope(
    exc: openai.APIError,
    provider_id: str,
    model_id: str,
) -> ProviderErrorEnvelope:
    status = getattr(exc, "status_code", None)
    raw: Dict[str, Any] = {}
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        raw = body
    elif isinstance(body, (str, bytes)):
        try:
            raw = json.loads(body)
        except Exception:
            raw = {"raw_body": str(body)}
    request_id = getattr(exc, "request_id", None)
    return ProviderErrorEnvelope(
        provider_id=provider_id,
        model_id=model_id,
        error_type=_classify_openai_error(exc) if isinstance(exc, openai.APIStatusError) else "unknown",
        message=str(exc),
        http_status=status,
        raw_json=raw,
        request_id=str(request_id) if request_id else None,
        timestamp=time.time(),
    )


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

        api_key = os.getenv(provider_config.api_key_env)
        if not api_key:
            raise ValueError(
                f"OpenAIProvider: env var {provider_config.api_key_env} is not set"
            )

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

    @property
    def tier(self) -> ModelTier:
        return self._handle.tier

    @property
    def pricing(self) -> Optional[ModelPricing]:
        return self._handle.pricing

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
            msg_dict: Dict[str, Any] = {"role": m.role, "content": m.content}
            # Restore reasoning_content for providers that require it (e.g. DeepSeek)
            rc = (m.metadata or {}).get("reasoning_content")
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
        oai_msgs = self._to_openai_messages(messages, system_instruction)

        try:
            response = await self._client.chat.completions.create(
                model=self.model_name,
                messages=oai_msgs,
                max_tokens=kwargs.pop("max_tokens", _DEFAULT_MAX_TOKENS),
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

    async def chat_with_tools(
        self,
        messages: List[ModelMessage],
        tools: List[Any],  # List[ToolSchema]
        system_instruction: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Any], ModelUsage]:
        start = time.time()
        oai_msgs: List[Dict[str, Any]] = self._to_openai_messages(messages, system_instruction)
        oai_tools = [t.to_openai_function() for t in tools] if tools else []

        total_prompt = total_completion = total_cached = 0
        tool_results: List[Any] = []
        final_text = ""
        last_reasoning_content: Optional[str] = None

        for _ in range(_MAX_TOOL_ITERATIONS):
            create_kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "messages": oai_msgs,
                "max_tokens": kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
            }
            if oai_tools:
                create_kwargs["tools"] = oai_tools
                create_kwargs["tool_choice"] = "auto"

            try:
                response = await self._client.chat.completions.create(**create_kwargs)
            except openai.APIError as exc:
                env = _make_envelope(exc, self.provider_id, self.model_name)
                _err_usage = ModelUsage(
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                    total_tokens=total_prompt + total_completion,
                    cached_tokens=total_cached,
                    request_time=time.time() - start,
                )
                try:
                    await self._record_usage(
                        session_id=kwargs.get("session_id", "default"),
                        usage=_err_usage,
                        latency_ms=int((time.time() - start) * 1000),
                        success=False,
                        error_envelope=env,
                    )
                except Exception:
                    pass
                raise ProviderInvocationError(env) from exc

            if response.usage:
                total_prompt += response.usage.prompt_tokens
                total_completion += response.usage.completion_tokens
                total_cached += self._extract_cached_tokens(response)

            msg = response.choices[0].message
            if msg.content:
                final_text += msg.content
            last_reasoning_content = getattr(msg, "reasoning_content", None) or None

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
            # DeepSeek reasoning models return reasoning_content that must be
            # echoed back verbatim in the next request or the API raises 400.
            _reasoning = getattr(msg, "reasoning_content", None)
            if _reasoning:
                assistant_turn["reasoning_content"] = _reasoning
            oai_msgs.append(assistant_turn)

            # Execute each tool call
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                tr, payload = await self._execute_tool(tc.function.name, args)
                tool_results.append(tr)
                oai_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(payload),
                    }
                )
        else:
            logger.warning("OpenAIProvider: tool loop hit max_iterations=%d", _MAX_TOOL_ITERATIONS)

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
        **kwargs: Any,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        oai_msgs = self._to_openai_messages(messages, system_instruction)

        try:
            async with self._client.chat.completions.stream(
                model=self.model_name,
                messages=oai_msgs,
                max_tokens=kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
            ) as stream:
                async for event in stream:
                    if event.type == "content.delta":
                        yield UnifiedStreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=event.content,
                        )
                    elif event.type == "chunk":
                        # Low-level chunk: try to extract content delta
                        try:
                            delta = event.chunk.choices[0].delta
                            if delta.content:
                                yield UnifiedStreamEvent(
                                    type=StreamEventType.TEXT_DELTA,
                                    text=delta.content,
                                )
                        except (AttributeError, IndexError):
                            pass

                completion = await stream.get_final_completion()
                usage = completion.usage
                if usage:
                    cached = self._extract_cached_tokens(completion)
                    snap = ModelUsageSnapshot(
                        input_tokens=usage.prompt_tokens,
                        output_tokens=usage.completion_tokens,
                        cached_tokens=cached,
                        cost_usd=self.estimate_cost(
                            ModelUsage(
                                prompt_tokens=usage.prompt_tokens,
                                completion_tokens=usage.completion_tokens,
                                cached_tokens=cached,
                            )
                        ),
                    )
                    yield UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL, usage=snap)
        except openai.APIError as exc:
            envelope = _make_envelope(exc, self.provider_id, self.model_name)
            yield UnifiedStreamEvent(type=StreamEventType.ERROR, error_envelope=envelope)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_cached_tokens(response: Any) -> int:
        try:
            return response.usage.prompt_tokens_details.cached_tokens or 0
        except AttributeError:
            return 0

    async def _execute_tool(
        self, name: str, args: Dict[str, Any]
    ) -> Tuple[Any, Dict[str, Any]]:
        from deile.tools.base import ToolContext, ToolResult, ToolStatus
        from deile.tools.registry import get_tool_registry

        registry = get_tool_registry()
        tool = registry.get(name)
        if tool is None:
            available = sorted(registry._tools.keys())
            payload = {
                "error": f"Tool '{name}' not found. Available: {', '.join(available)}",
                "status": "error",
            }
            return ToolResult(status=ToolStatus.ERROR, message=payload["error"]), payload

        ctx = ToolContext(user_input="", parsed_args=dict(args or {}))
        try:
            result = await tool.execute(ctx)
        except Exception as exc:
            payload = {"error": str(exc), "status": "error"}
            return ToolResult(status=ToolStatus.ERROR, message=str(exc), error=exc), payload

        if result.is_success:
            payload = {"status": "success", "result": str(result.data) if result.data is not None else ""}
        else:
            payload = {"status": "error", "error": result.message or f"{name} failed"}
        return result, payload
