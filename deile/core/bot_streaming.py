"""Structured + chunked output DTOs for bot consumers (plano DEILE fase 3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Mapping

from deile.common.markup_ast import MarkupAST


ChunkKind = Literal[
    "text",
    "markup_span",
    "tool_call_started",
    "tool_call_finished",
    "done",
    "error",
]


@dataclass(frozen=True)
class StreamChunk:
    """Typed event emitted by `process_input_stream_chunks`.

    `payload` shape varies by `kind`:
    - text: {"text": str, "incremental": True}
    - markup_span: {"kind": str, "text": str, "meta": Mapping}
    - tool_call_started: {"tool_name": str, "args_preview": str}
    - tool_call_finished: {"tool_name": str, "ok": bool, "elapsed_ms": int}
    - done: {"text": str, "markup": MarkupAST, "elapsed_ms": int, "model_used": str}
    - error: {"type": str, "message": str}
    """

    kind: ChunkKind
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    ok: bool
    elapsed_ms: int = 0
    args_preview: str = ""


@dataclass(frozen=True)
class StructuredResponse:
    """Structured wrapper around an AgentResponse for bot consumers."""

    text: str
    markup: MarkupAST
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    elapsed_ms: int = 0
    model_used: str = ""
    status: str = "idle"
