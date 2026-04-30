"""Anthropic (Claude) provider — multi-provider router implementation."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import anthropic

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


def _classify_anthropic_error(exc: anthropic.APIError) -> str:
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
    exc: anthropic.APIError,
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
    if request_id is None:
        headers = getattr(exc, "response", None)
        if headers is not None:
            request_id = getattr(headers, "headers", {}).get("request-id")
    return ProviderErrorEnvelope(
        provider_id=provider_id,
        model_id=model_id,
        error_type=_classify_anthropic_error(exc),
        message=str(exc),
        http_status=status,
        raw_json=raw,
        request_id=str(request_id) if request_id else None,
        timestamp=time.time(),
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

        api_key = os.getenv(provider_config.api_key_env)
        if not api_key:
            raise ValueError(
                f"AnthropicProvider: env var {provider_config.api_key_env} is not set"
            )

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

    @property
    def tier(self) -> ModelTier:
        return self._handle.tier

    @property
    def pricing(self) -> Optional[ModelPricing]:
        return self._handle.pricing

    # ------------------------------------------------------------------
    # Message conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_anthropic_messages(
        messages: List[ModelMessage],
    ) -> List[Dict[str, Any]]:
        """Convert ModelMessage list → Anthropic messages array (no system)."""
        result = []
        for m in messages:
            if m.role == "system":
                continue
            result.append({"role": m.role, "content": m.content})
        return result

    @staticmethod
    def _extract_system(messages: List[ModelMessage], system_instruction: Optional[str]) -> Optional[str]:
        sys_from_msgs = next((m.content for m in messages if m.role == "system"), None)
        return system_instruction or sys_from_msgs

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
        system = self._extract_system(messages, system_instruction)
        anthropic_msgs = self._to_anthropic_messages(messages)

        create_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": kwargs.pop("max_tokens", _DEFAULT_MAX_TOKENS),
            "messages": anthropic_msgs,
        }
        if system:
            create_kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]

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

    async def chat_with_tools(
        self,
        messages: List[ModelMessage],
        tools: List[Any],  # List[ToolSchema]
        system_instruction: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Any], ModelUsage]:
        from deile.tools.base import ToolContext, ToolStatus

        start = time.time()
        system = self._extract_system(messages, system_instruction)
        anthropic_msgs: List[Dict[str, Any]] = self._to_anthropic_messages(messages)
        anthropic_tools = [t.to_anthropic_tool() for t in tools] if tools else []

        total_input = total_output = total_cached = 0
        tool_results: List[Any] = []
        final_text = ""

        for iteration in range(_MAX_TOOL_ITERATIONS):
            create_kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "max_tokens": kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
                "messages": anthropic_msgs,
            }
            if system:
                create_kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            if anthropic_tools:
                create_kwargs["tools"] = anthropic_tools

            try:
                response = await self._client.messages.create(**create_kwargs)
            except anthropic.APIError as exc:
                raise ProviderInvocationError(
                    _make_envelope(exc, self.provider_id, self.model_name)
                ) from exc

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
                tr, payload = await self._execute_tool(block.name, block.input)
                tool_results.append(tr)
                tool_result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                    }
                )

            anthropic_msgs.append({"role": "user", "content": tool_result_content})
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
        **kwargs: Any,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        system = self._extract_system(messages, system_instruction)
        anthropic_msgs = self._to_anthropic_messages(messages)

        create_kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
            "messages": anthropic_msgs,
        }
        if system:
            create_kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]

        try:
            async with self._client.messages.stream(**create_kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta and getattr(delta, "type", None) == "text_delta":
                            yield UnifiedStreamEvent(
                                type=StreamEventType.TEXT_DELTA,
                                text=delta.text,
                            )
                    elif event.type == "message_stop":
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
                        )
                        yield UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL, usage=snap)
        except anthropic.APIError as exc:
            envelope = _make_envelope(exc, self.provider_id, self.model_name)
            yield UnifiedStreamEvent(type=StreamEventType.ERROR, error_envelope=envelope)

    # ------------------------------------------------------------------
    # Internal tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self, name: str, args: Dict[str, Any]
    ) -> Tuple[Any, Dict[str, Any]]:
        """Run one tool via ToolRegistry; return (ToolResult, json-serialisable payload)."""
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
            return (
                ToolResult(status=ToolStatus.ERROR, message=str(exc), error=exc),
                payload,
            )

        if result.is_success:
            payload = {"status": "success", "result": str(result.data) if result.data is not None else ""}
        else:
            payload = {"status": "error", "error": result.message or f"{name} failed"}
        return result, payload
