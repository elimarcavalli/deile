"""Tests for :mod:`deile.ui.panel.observability.jsonl_parser` (issue #347).

Goal: enforce the contract documented at the top of ``jsonl_parser.py``,
which the rest of the observability panel relies on:

* Every observed turn type round-trips into its dataclass.
* Malformed JSONL lines are skipped and counted, never crash the parser.
* ``parse_tail(since_byte_offset=...)`` resumes correctly.
* A ``tool_use`` without a matching ``tool_result`` is flagged ``in_progress``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deile.ui.panel.observability.jsonl_parser import (
    AssistantTurn,
    ClaudeJsonlParser,
    ToolResultTurn,
    ToolUseTurn,
    UnknownTurn,
    UserTurn,
)


def _write_jsonl(path: Path, rows) -> Path:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_parses_user_turn(tmp_path):
    """A ``{"type":"user"}`` line becomes a :class:`UserTurn`."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "user", "content": "implement issue 347", "ts": "2026-05-27T16:04:10Z"},
    ])
    result = ClaudeJsonlParser(p).parse_all()
    assert len(result.turns) == 1
    turn = result.turns[0]
    assert isinstance(turn, UserTurn)
    assert turn.content == "implement issue 347"
    assert turn.role == "user"
    assert turn.ts == "2026-05-27T16:04:10Z"
    assert turn.in_progress is False


def test_parses_assistant_turn_with_usage(tmp_path):
    """An ``{"type":"assistant"}`` line captures content/model/usage."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "assistant",
            "content": "Vou analisar a PR.",
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3120, "output_tokens": 1703},
        },
    ])
    result = ClaudeJsonlParser(p).parse_all()
    turn = result.turns[0]
    assert isinstance(turn, AssistantTurn)
    assert turn.model == "claude-sonnet-4-6"
    assert turn.stop_reason == "end_turn"
    assert turn.usage["output_tokens"] == 1703
    assert turn.content == "Vou analisar a PR."


def test_parses_tool_use_turn(tmp_path):
    """A ``tool_use`` becomes :class:`ToolUseTurn` with name/input."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "Bash",
            "input": {"command": "gh pr view 346"},
        },
    ])
    result = ClaudeJsonlParser(p).parse_all()
    turn = result.turns[0]
    assert isinstance(turn, ToolUseTurn)
    assert turn.tool_name == "Bash"
    assert turn.tool_use_id == "toolu_01"
    assert turn.tool_input["command"] == "gh pr view 346"


def test_parses_tool_result_turn(tmp_path):
    """A ``tool_result`` becomes :class:`ToolResultTurn` with is_error+content."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_01",
            "is_error": False,
            "content": "files changed: 3",
        },
    ])
    result = ClaudeJsonlParser(p).parse_all()
    turn = result.turns[0]
    assert isinstance(turn, ToolResultTurn)
    assert turn.tool_use_id == "toolu_01"
    assert turn.is_error is False
    assert turn.content == "files changed: 3"


def test_handles_unknown_turn_type(tmp_path):
    """Unknown ``type`` values fall back to :class:`UnknownTurn`."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "system", "content": "compacting context..."},
    ])
    result = ClaudeJsonlParser(p).parse_all()
    turn = result.turns[0]
    assert isinstance(turn, UnknownTurn)
    assert turn.type_label == "system"
    assert "compacting" in turn.summary


def test_skips_malformed_line(tmp_path):
    """Lines that are not valid JSON are skipped and counted."""
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"type":"user","content":"ok"}\n'
        "not-json-at-all\n"
        '{"type":"assistant","content":"reply"}\n',
        encoding="utf-8",
    )
    result = ClaudeJsonlParser(p).parse_all()
    assert len(result.turns) == 2
    assert result.skipped_malformed_lines == 1


def test_tail_incremental_from_offset(tmp_path):
    """``parse_tail`` with ``since_byte_offset`` returns only new turns."""
    p = tmp_path / "s.jsonl"
    p.write_text('{"type":"user","content":"first"}\n', encoding="utf-8")

    first = ClaudeJsonlParser(p).parse_all()
    assert len(first.turns) == 1
    offset = first.next_offset

    # Append a second turn — only that one should come back.
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","content":"second"}\n')

    second = ClaudeJsonlParser(p).parse_tail(since_byte_offset=offset)
    assert len(second.turns) == 1
    assert isinstance(second.turns[0], AssistantTurn)
    assert second.turns[0].content == "second"


def test_marks_in_progress_tool_call(tmp_path):
    """A ``tool_use`` without a matching ``tool_result`` is in_progress=True."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "tool_use", "id": "toolu_42", "name": "Bash", "input": {}},
    ])
    result = ClaudeJsonlParser(p).parse_all()
    turn = result.turns[0]
    assert isinstance(turn, ToolUseTurn)
    assert turn.in_progress is True


def test_marks_completed_when_result_present(tmp_path):
    """A ``tool_use`` paired with its result is NOT in_progress."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "tool_use", "id": "toolu_99", "name": "Read", "input": {}},
        {"type": "tool_result", "tool_use_id": "toolu_99", "content": "ok"},
    ])
    result = ClaudeJsonlParser(p).parse_all()
    tu = result.turns[0]
    assert isinstance(tu, ToolUseTurn)
    assert tu.in_progress is False


def test_returns_empty_when_file_missing(tmp_path):
    """Missing JSONL file is treated as empty — never raises."""
    result = ClaudeJsonlParser(tmp_path / "does-not-exist.jsonl").parse_all()
    assert result.turns == []
    assert result.next_offset == 0


def test_offset_beyond_eof_rewinds(tmp_path):
    """``since_byte_offset`` past EOF (rotated/truncated) reads from start."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "user", "content": "after-rotation"},
    ])
    # Offset far past EOF — must NOT raise and must NOT return nothing.
    result = ClaudeJsonlParser(p).parse_tail(since_byte_offset=10_000_000)
    assert len(result.turns) == 1
    assert isinstance(result.turns[0], UserTurn)


def test_caps_to_max_turns_keeping_latest(tmp_path):
    """``max_turns`` keeps the *latest* turns when overflowing."""
    rows = [{"type": "user", "content": f"msg-{i}"} for i in range(10)]
    p = _write_jsonl(tmp_path / "s.jsonl", rows)
    result = ClaudeJsonlParser(p).parse_all(max_turns=3)
    assert len(result.turns) == 3
    contents = [t.content for t in result.turns]
    assert contents == ["msg-7", "msg-8", "msg-9"]


def test_rejects_non_positive_max_turns(tmp_path):
    """Invalid ``max_turns`` raises ValueError — parser refuses to mis-behave."""
    p = _write_jsonl(tmp_path / "s.jsonl", [{"type": "user", "content": "x"}])
    parser = ClaudeJsonlParser(p)
    with pytest.raises(ValueError):
        parser.parse_all(max_turns=0)


def test_content_blocks_coerced_to_string(tmp_path):
    """Anthropic-style content blocks (list of dicts with ``text``) become str."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "assistant", "content": [
            {"type": "text", "text": "first line"},
            {"type": "text", "text": "second line"},
        ]},
    ])
    result = ClaudeJsonlParser(p).parse_all()
    turn = result.turns[0]
    assert "first line" in turn.content
    assert "second line" in turn.content
