"""Parser for the Claude CLI session JSONL files (issue #347).

The ``claude -p`` command writes the full conversation to
``~/.claude/projects/<workspace-hash>/<session-uuid>.jsonl`` — one JSON
object per line.  Observed turn types in May/2026:

* ``user``         — user (or harness) message turn
* ``assistant``    — model reply, including ``usage`` block
* ``tool_use``     — model invoked a tool (Bash, Read, ...)
* ``tool_result``  — output of a previous ``tool_use``
* ``system``/other — informational frames (compaction, hooks, ...)

The parser must be tolerant of partial writes (last line may be incomplete
during a live tail) and supports incremental reads via :meth:`parse_tail`
(``since_byte_offset`` + max-turns cap so the renderer cannot be saturated).
A turn that has a ``tool_use`` *without* its matching ``tool_result`` is
flagged ``in_progress=True`` so the screen can mark it with ``▶``.

The parser is deliberately a thin transformation layer — no Rich, no aiohttp,
no asyncio — so it can be unit-tested with plain ``open()`` fixtures.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


@dataclass
class _BaseTurn:
    """Shared metadata for every parsed turn."""

    index: int
    """0-based ordinal within the parsed window (NOT the absolute line)."""

    ts: Optional[str]
    """ISO-8601 timestamp if present in the JSONL line; ``None`` otherwise."""

    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    """Original JSON object (useful for the ``[j]`` raw view in the panel)."""

    in_progress: bool = False
    """``True`` for a ``tool_use`` whose ``tool_result`` is missing — used by
    :class:`LiveSessionScreen` to mark the cursor with ``▶``.
    """


@dataclass
class UserTurn(_BaseTurn):
    """A ``{"type":"user", "content":...}`` line."""

    content: str = ""

    @property
    def role(self) -> str:
        return "user"


@dataclass
class AssistantTurn(_BaseTurn):
    """An ``{"type":"assistant", ...}`` line.

    Captures ``content``, ``stop_reason`` and the ``usage`` dict (verbatim).
    The renderer slices long content with its own ellipsis policy.
    """

    content: str = ""
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def role(self) -> str:
        return "assistant"


@dataclass
class ToolUseTurn(_BaseTurn):
    """An ``{"type":"tool_use", "name":..., "input":{...}}`` line."""

    tool_use_id: str = ""
    tool_name: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)

    @property
    def role(self) -> str:
        return "tool"


@dataclass
class ToolResultTurn(_BaseTurn):
    """An ``{"type":"tool_result", "tool_use_id":..., "content":...}`` line."""

    tool_use_id: str = ""
    is_error: bool = False
    content: str = ""

    @property
    def role(self) -> str:
        return "result"


@dataclass
class UnknownTurn(_BaseTurn):
    """Fallback for turn types we did not (yet) special-case.

    Kept verbose enough that the operator can see *something* on screen
    instead of an empty row (e.g. ``system`` / ``compact`` / ``hook`` frames).
    """

    type_label: str = "unknown"
    summary: str = ""

    @property
    def role(self) -> str:
        return self.type_label


Turn = Union[UserTurn, AssistantTurn, ToolUseTurn, ToolResultTurn, UnknownTurn]


@dataclass
class TailResult:
    """Outcome of :meth:`ClaudeJsonlParser.parse_tail`."""

    turns: List[Turn]
    next_offset: int
    """Byte offset to pass on the next incremental call (file size at EOF)."""

    skipped_malformed_lines: int = 0


class ClaudeJsonlParser:
    """Parser for the Claude CLI session JSONL files.

    The parser holds no file handles between calls — every method re-opens
    the file in read-binary mode (so we can ``seek`` to a byte offset that
    survives encoding changes).  This keeps the parser usable from sync
    contexts as well as ``asyncio.to_thread``.
    """

    DEFAULT_MAX_TURNS = 50
    """Cap applied to :meth:`parse_tail` when ``max_turns`` is not supplied."""

    def __init__(self, path: Path, *, encoding: str = "utf-8"):
        self.path = Path(path)
        self.encoding = encoding

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def parse_all(self, *, max_turns: Optional[int] = None) -> TailResult:
        """Parse the entire file from the start.

        Convenience helper around :meth:`parse_tail`.  ``max_turns`` defaults
        to :attr:`DEFAULT_MAX_TURNS` and the latest turns win on overflow
        (the operator typically wants the tail of the conversation).
        """
        return self.parse_tail(since_byte_offset=0, max_turns=max_turns)

    def parse_tail(
        self,
        *,
        since_byte_offset: int = 0,
        max_turns: Optional[int] = None,
    ) -> TailResult:
        """Parse turns starting at ``since_byte_offset``.

        Args:
            since_byte_offset: Resume position from a previous call.  ``0``
                rewinds to the start.  Values larger than the current file
                size are treated as ``0`` (file was rotated/truncated).
            max_turns: Cap on the number of turns returned.  When the file
                contains more, the *latest* ``max_turns`` are kept so the
                operator sees the current state of the conversation.

        Returns:
            :class:`TailResult` with parsed turns, the byte offset to use
            for the next incremental call (always the end-of-file byte), and
            the count of skipped malformed lines.
        """
        if max_turns is None:
            max_turns = self.DEFAULT_MAX_TURNS
        if max_turns <= 0:
            raise ValueError(f"max_turns must be positive, got {max_turns!r}")

        if not self.path.exists():
            return TailResult(turns=[], next_offset=0, skipped_malformed_lines=0)

        try:
            file_size = self.path.stat().st_size
        except OSError as exc:
            logger.warning("stat(%s) failed: %s", self.path, exc)
            return TailResult(turns=[], next_offset=0, skipped_malformed_lines=0)

        start = since_byte_offset if 0 <= since_byte_offset <= file_size else 0

        raw_lines: List[str] = []
        try:
            with self.path.open("rb") as fh:
                if start:
                    fh.seek(start)
                # Read by lines using bytes to keep precise EOF accounting.
                for raw in fh:
                    raw_lines.append(raw.decode(self.encoding, "replace"))
        except OSError as exc:
            logger.warning("read(%s) failed: %s", self.path, exc)
            return TailResult(turns=[], next_offset=start, skipped_malformed_lines=0)

        parsed: List[Tuple[int, Dict[str, Any]]] = []
        skipped = 0
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            parsed.append((len(parsed), obj))

        # Keep the latest ``max_turns`` so the live tail always reflects the
        # current state of the conversation rather than dropping the most
        # recent activity.
        if len(parsed) > max_turns:
            parsed = parsed[-max_turns:]

        turns: List[Turn] = [self._parse_object(idx, obj) for idx, obj in parsed]
        self._mark_in_progress(turns)

        return TailResult(
            turns=turns,
            next_offset=file_size,
            skipped_malformed_lines=skipped,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _parse_object(self, index: int, obj: Dict[str, Any]) -> Turn:
        """Dispatch on ``type`` to the right turn dataclass."""
        ttype = (obj.get("type") or "").lower()
        ts = self._extract_ts(obj)

        if ttype == "user":
            return UserTurn(
                index=index,
                ts=ts,
                raw=obj,
                content=self._coerce_content(obj.get("content") or obj.get("text")),
            )
        if ttype == "assistant":
            return AssistantTurn(
                index=index,
                ts=ts,
                raw=obj,
                content=self._coerce_content(obj.get("content") or obj.get("text")),
                model=obj.get("model"),
                stop_reason=obj.get("stop_reason"),
                usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else {},
            )
        if ttype == "tool_use":
            return ToolUseTurn(
                index=index,
                ts=ts,
                raw=obj,
                tool_use_id=str(obj.get("id") or obj.get("tool_use_id") or ""),
                tool_name=str(obj.get("name") or ""),
                tool_input=(
                    obj.get("input") if isinstance(obj.get("input"), dict) else {}
                ),
            )
        if ttype == "tool_result":
            return ToolResultTurn(
                index=index,
                ts=ts,
                raw=obj,
                tool_use_id=str(obj.get("tool_use_id") or ""),
                is_error=bool(obj.get("is_error") or False),
                content=self._coerce_content(obj.get("content")),
            )

        summary = self._coerce_content(obj.get("content")) or json.dumps(obj)[:200]
        return UnknownTurn(
            index=index,
            ts=ts,
            raw=obj,
            type_label=ttype or "unknown",
            summary=summary,
        )

    @staticmethod
    def _extract_ts(obj: Dict[str, Any]) -> Optional[str]:
        """Try a couple of common timestamp keys; tolerate int/float epochs."""
        for key in ("ts", "timestamp", "time", "created_at"):
            raw = obj.get(key)
            if raw is None:
                continue
            if isinstance(raw, (int, float)):
                try:
                    return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
                except (OSError, OverflowError, ValueError):
                    continue
            if isinstance(raw, str):
                return raw
        return None

    @staticmethod
    def _coerce_content(value: Any) -> str:
        """Best-effort string coercion (Claude sometimes nests content blocks)."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: List[str] = []
            for item in value:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                parts.append(json.dumps(item)[:200])
            return "\n".join(parts)
        return str(value)

    @staticmethod
    def _mark_in_progress(turns: List[Turn]) -> None:
        """Mark any ``tool_use`` without a matching ``tool_result`` as in-flight."""
        seen_results: Dict[str, bool] = {}
        for turn in turns:
            if isinstance(turn, ToolResultTurn) and turn.tool_use_id:
                seen_results[turn.tool_use_id] = True
        for turn in turns:
            if isinstance(turn, ToolUseTurn) and turn.tool_use_id:
                turn.in_progress = not seen_results.get(turn.tool_use_id, False)
