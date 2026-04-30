"""Unified streaming event types for all providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StreamEventType(Enum):
    TEXT_DELTA = "text_delta"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_DELTA = "tool_use_delta"
    TOOL_USE_END = "tool_use_end"
    USAGE_FINAL = "usage_final"
    ERROR = "error"


@dataclass
class ModelUsageSnapshot:
    """Token/cost snapshot emitted with USAGE_FINAL events."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class UnifiedStreamEvent:
    """Single event in a provider's unified stream."""

    type: StreamEventType

    # TEXT_DELTA
    text: Optional[str] = None

    # TOOL_USE_*
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None                    # TOOL_USE_START
    arguments_json_delta: Optional[str] = None         # TOOL_USE_DELTA
    arguments: Optional[Dict[str, Any]] = None         # TOOL_USE_END (parsed)

    # USAGE_FINAL
    usage: Optional[ModelUsageSnapshot] = None

    # ERROR
    error_envelope: Optional[Any] = None               # ProviderErrorEnvelope (avoids circular import)
