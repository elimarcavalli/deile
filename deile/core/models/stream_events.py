"""Unified streaming event types for all providers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class StreamEventType(Enum):
    TEXT_DELTA = "text_delta"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_DELTA = "tool_use_delta"
    TOOL_USE_END = "tool_use_end"
    TOOL_RESULT = "tool_result"
    USAGE_FINAL = "usage_final"
    ERROR = "error"
    STAGE = "stage"
    PROGRESS = "progress"
    RICH_RENDERABLE = "rich_renderable"


@dataclass
class ModelUsageSnapshot:
    """Token/cost snapshot emitted with USAGE_FINAL events."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    # Identifier of the model that actually generated the response — surfaced
    # in the streaming footer so the user knows who replied (e.g. when the
    # tier router falls back across providers).
    model: str = ""


@dataclass
class UnifiedStreamEvent:
    """Single event in a provider's unified stream."""

    type: StreamEventType

    # TEXT_DELTA
    text: Optional[str] = None

    # TOOL_USE_*
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None  # TOOL_USE_START, TOOL_RESULT
    arguments_json_delta: Optional[str] = None  # TOOL_USE_DELTA
    arguments: Optional[Dict[str, Any]] = None  # TOOL_USE_END (parsed)

    # TOOL_RESULT
    tool_status: Optional[str] = None  # "success" | "error" | "running"
    tool_result_summary: Optional[str] = None  # short preview (≤ 200 chars)
    tool_result_data: Optional[Any] = None  # raw payload for rich display
    tool_metadata: Optional[Dict[str, Any]] = (
        None  # full ToolResult.metadata copy — carries
    )
    # post_write_validation_required, file_path,
    # etc. so downstream gates that inspect
    # metadata keep working after the streaming
    # reconstruction round-trip

    # USAGE_FINAL
    usage: Optional[ModelUsageSnapshot] = None

    # ERROR
    error_envelope: Optional[Any] = (
        None  # ProviderErrorEnvelope (avoids circular import)
    )

    # Tool-loop iteration (set by the agent's tool-loop executor)
    iteration: Optional[int] = None

    # Source marker for downstream UI (e.g. "validation_gate")
    source: Optional[str] = None

    # Provider-specific reasoning/thinking content (e.g. DeepSeek reasoning models).
    # Must be echoed verbatim in the next API call's assistant message.
    reasoning_content: Optional[str] = None

    # STAGE — short label describing what the agent is currently doing
    # before the LLM starts streaming (e.g. "Analyzing intent",
    # "Connecting to deepseek-v4-pro", "Awaiting first token").
    stage: Optional[str] = None

    # PROGRESS — incremental counter for long-running operations
    progress_current: Optional[int] = None
    progress_total: Optional[int] = None
    progress_label: Optional[str] = None

    # RICH_RENDERABLE — the Rich object (Table, Panel, Group, …) to print
    # as-is. The renderer must call ``console.print(renderable)`` so
    # Rich's width-aware layout runs at the actual terminal width.
    renderable: Optional[Any] = None
