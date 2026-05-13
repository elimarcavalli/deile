"""Tests for the rewritten /clear default behavior.

The default ``/cls`` flow archives the current conversation, spawns a
fresh session, and writes the ``_switch_session`` + ``_post_switch_action``
sentinels into the current session's ``context_data`` so the CLI redraws
the welcome banner. Advanced subcommands (``reset``, ``history``,
``screen``) keep their legacy behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from deile.commands.base import CommandContext
from deile.commands.builtin._session_store import SessionHistoryStore
from deile.commands.builtin.clear_command import ClearCommand

_HISTORY = [
    {"role": "user", "content": "olá", "timestamp": 1.0, "metadata": {}},
    {"role": "assistant", "content": "oi!", "timestamp": 1.1, "metadata": {}},
]


def _make_session(session_id: str = "current-sid") -> MagicMock:
    s = MagicMock()
    s.session_id = session_id
    s.conversation_history = list(_HISTORY)
    s.context_data = {}
    s.working_directory = Path("/tmp")
    return s


def _make_agent() -> MagicMock:
    agent = MagicMock()
    _sessions: Dict[str, Any] = {}

    def _create_session(session_id: str, **kwargs: Any) -> MagicMock:
        sess = MagicMock()
        sess.session_id = session_id
        sess.conversation_history = []
        sess.context_data = {}
        sess.working_directory = kwargs.get("working_directory", Path("/tmp"))
        _sessions[session_id] = sess
        return sess

    def _get_session(session_id: str) -> Optional[MagicMock]:
        return _sessions.get(session_id)

    agent.create_session.side_effect = _create_session
    agent.get_session.side_effect = _get_session
    return agent


def _make_context(args: str = "") -> CommandContext:
    ctx = CommandContext(user_input=f"/cls {args}".strip(), args=args)
    ctx.agent = _make_agent()
    ctx.session = _make_session()
    return ctx


class TestClearDefaultBehavior:
    @pytest.mark.unit
    async def test_default_archives_current_conversation(self, tmp_path):
        ctx = _make_context()
        save_mock = MagicMock()
        with patch.object(SessionHistoryStore, "save", save_mock):
            result = await ClearCommand().execute(ctx)
        assert result.success
        save_mock.assert_called_once()
        saved_sid, saved_history, _ = save_mock.call_args.args
        assert saved_sid == "current-sid"
        assert saved_history == _HISTORY

    @pytest.mark.unit
    async def test_default_creates_new_session_and_sets_sentinels(self):
        ctx = _make_context()
        with patch.object(SessionHistoryStore, "save"):
            result = await ClearCommand().execute(ctx)
        assert result.success
        new_sid = ctx.session.context_data.get("_switch_session")
        assert new_sid is not None and new_sid != "current-sid"
        assert new_sid.startswith("clear-")
        assert ctx.session.context_data.get("_post_switch_action") == "welcome"
        assert ctx.agent.get_session(new_sid) is not None

    @pytest.mark.unit
    async def test_default_suppresses_response_display(self):
        ctx = _make_context()
        with patch.object(SessionHistoryStore, "save"):
            result = await ClearCommand().execute(ctx)
        assert (result.metadata or {}).get("suppress_response_display") is True

    @pytest.mark.unit
    async def test_default_skips_archive_when_history_empty(self):
        ctx = _make_context()
        ctx.session.conversation_history = []
        save_mock = MagicMock()
        with patch.object(SessionHistoryStore, "save", save_mock):
            result = await ClearCommand().execute(ctx)
        assert result.success
        save_mock.assert_not_called()
        # new session is still created
        assert ctx.session.context_data.get("_switch_session") is not None

    @pytest.mark.unit
    async def test_default_falls_back_when_no_agent(self):
        """``deile --clear`` (one-shot) has no agent/session; must not raise."""
        ctx = CommandContext(user_input="/cls", args="")
        ctx.agent = None
        ctx.session = None
        result = await ClearCommand().execute(ctx)
        assert result.success
        assert "_switch_session" not in (getattr(ctx, "session", {}) or {})

    @pytest.mark.unit
    async def test_default_preserves_conversation_name_in_archive(self):
        ctx = _make_context()
        ctx.session.context_data["conversation_name"] = "Investigação ESC"
        save_mock = MagicMock()
        with patch.object(SessionHistoryStore, "save", save_mock):
            await ClearCommand().execute(ctx)
        _, _, saved_name = save_mock.call_args.args
        assert saved_name == "Investigação ESC"


class TestClearSubcommandsBackwardCompat:
    @pytest.mark.unit
    async def test_history_subcommand_still_works(self):
        ctx = _make_context("history")
        ctx.agent.clear_conversation_history = MagicMock()
        result = await ClearCommand().execute(ctx)
        assert result.success
        # Old behavior: no session switch
        assert ctx.session.context_data.get("_switch_session") is None

    @pytest.mark.unit
    async def test_screen_subcommand_still_works(self):
        ctx = _make_context("screen")
        ctx.ui_manager = MagicMock()
        result = await ClearCommand().execute(ctx)
        assert result.success
        assert ctx.session.context_data.get("_switch_session") is None
