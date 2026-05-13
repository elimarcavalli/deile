"""Tests for /fork, /rename, /rewind, /resume conversation commands."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.commands.base import CommandContext
from deile.commands.builtin._conv_store import ConversationNameStore
from deile.commands.builtin._session_store import SessionHistoryStore
from deile.commands.builtin.fork_command import ForkCommand
from deile.commands.builtin.rename_command import RenameCommand
from deile.commands.builtin.resume_command import ResumeCommand
from deile.commands.builtin.rewind_command import RewindCommand
from deile.memory.episodic_memory import EpisodicMemory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HISTORY = [
    {"role": "user", "content": "olá mundo", "timestamp": 1.0, "metadata": {}},
    {"role": "assistant", "content": "Olá!", "timestamp": 1.1, "metadata": {}},
    {"role": "user", "content": "como vai?", "timestamp": 2.0, "metadata": {}},
    {"role": "assistant", "content": "Bem!", "timestamp": 2.1, "metadata": {}},
]


def _make_session(
    session_id: str = "test-sid",
    history: Optional[List[Dict[str, Any]]] = None,
) -> MagicMock:
    s = MagicMock()
    s.session_id = session_id
    s.conversation_history = list(history if history is not None else _HISTORY)
    s.context_data = {}
    s.working_directory = Path("/tmp")
    return s


def _make_agent(existing_sessions: Optional[Dict] = None) -> MagicMock:
    agent = MagicMock()
    _sessions: Dict[str, Any] = dict(existing_sessions or {})

    def _create_session(session_id, **kwargs):
        sess = MagicMock()
        sess.session_id = session_id
        sess.conversation_history = []
        sess.context_data = {}
        sess.working_directory = kwargs.get("working_directory", Path("/tmp"))
        _sessions[session_id] = sess
        return sess

    def _get_session(session_id):
        return _sessions.get(session_id)

    agent.create_session.side_effect = _create_session
    agent.get_session.side_effect = _get_session
    return agent


def _make_context(
    command: str,
    args: str = "",
    session_id: str = "test-sid",
    history: Optional[List] = None,
    agent: Optional[Any] = None,
    session: Optional[Any] = None,
) -> CommandContext:
    sess = session or _make_session(session_id=session_id, history=history)
    ag = agent or _make_agent()
    ctx = CommandContext(user_input=f"/{command} {args}".strip(), args=args)
    ctx.agent = ag
    ctx.session = sess
    return ctx


def _render(obj) -> str:
    from rich.console import Console
    buf = StringIO()
    Console(file=buf, highlight=False, markup=False, width=200).print(obj)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ConversationNameStore
# ---------------------------------------------------------------------------


class TestConversationNameStore:
    def test_get_missing(self, tmp_path):
        store = ConversationNameStore(path=tmp_path / "names.json")
        assert store.get("unknown") is None

    def test_set_and_get(self, tmp_path):
        store = ConversationNameStore(path=tmp_path / "names.json")
        store.set("sid1", "minha conversa")
        assert store.get("sid1") == "minha conversa"

    def test_overwrite(self, tmp_path):
        store = ConversationNameStore(path=tmp_path / "names.json")
        store.set("sid1", "alpha")
        store.set("sid1", "beta")
        assert store.get("sid1") == "beta"

    def test_all(self, tmp_path):
        store = ConversationNameStore(path=tmp_path / "names.json")
        store.set("a", "A")
        store.set("b", "B")
        assert store.all() == {"a": "A", "b": "B"}

    def test_delete(self, tmp_path):
        store = ConversationNameStore(path=tmp_path / "names.json")
        store.set("x", "X")
        store.delete("x")
        assert store.get("x") is None

    def test_delete_missing_is_noop(self, tmp_path):
        store = ConversationNameStore(path=tmp_path / "names.json")
        store.delete("nonexistent")  # should not raise

    def test_corrupted_file_returns_empty(self, tmp_path):
        p = tmp_path / "names.json"
        p.write_text("NOT JSON", encoding="utf-8")
        store = ConversationNameStore(path=p)
        assert store.get("x") is None


# ---------------------------------------------------------------------------
# ForkCommand
# ---------------------------------------------------------------------------


class TestForkCommand:
    @pytest.mark.unit
    async def test_fork_creates_new_session(self, tmp_path):
        ctx = _make_context("fork")
        with patch.object(ConversationNameStore, "set"):
            cmd = ForkCommand()
            result = await cmd.execute(ctx)

        assert result.success
        ctx.agent.create_session.assert_called_once()
        new_sid = ctx.session.context_data.get("_switch_session")
        assert new_sid is not None
        assert new_sid.startswith("fork-")

    @pytest.mark.unit
    async def test_fork_copies_history(self, tmp_path):
        ctx = _make_context("fork", history=_HISTORY)
        with patch.object(ConversationNameStore, "set"):
            cmd = ForkCommand()
            await cmd.execute(ctx)

        new_sid = ctx.session.context_data["_switch_session"]
        new_sess = ctx.agent.get_session(new_sid)
        assert len(new_sess.conversation_history) == len(_HISTORY)

    @pytest.mark.unit
    async def test_fork_with_name(self, tmp_path):
        ctx = _make_context("fork", args="minha feature")
        with patch.object(ConversationNameStore, "set") as mock_set:
            cmd = ForkCommand()
            result = await cmd.execute(ctx)

        assert result.success
        mock_set.assert_called_once()
        _, call_name = mock_set.call_args[0]
        assert call_name == "minha feature"

    @pytest.mark.unit
    async def test_fork_refuses_empty_conversation(self):
        ctx = _make_context("fork", history=[])
        cmd = ForkCommand()
        result = await cmd.execute(ctx)
        assert result.success
        assert "_switch_session" not in ctx.session.context_data
        ctx.agent.create_session.assert_not_called()

    @pytest.mark.unit
    async def test_fork_refuses_history_without_user_messages(self):
        history = [
            {"role": "system", "content": "sys prompt", "timestamp": 0.0, "metadata": {}},
            {"role": "assistant", "content": "olá", "timestamp": 0.1, "metadata": {}},
        ]
        ctx = _make_context("fork", history=history)
        cmd = ForkCommand()
        result = await cmd.execute(ctx)
        assert result.success
        assert "_switch_session" not in ctx.session.context_data
        ctx.agent.create_session.assert_not_called()

    @pytest.mark.unit
    async def test_fork_no_agent_returns_error(self):
        ctx = _make_context("fork")
        ctx.agent = None
        cmd = ForkCommand()
        result = await cmd.execute(ctx)
        assert not result.success

    @pytest.mark.unit
    async def test_fork_no_session_returns_error(self):
        ctx = _make_context("fork")
        ctx.session = None
        cmd = ForkCommand()
        result = await cmd.execute(ctx)
        assert not result.success


# ---------------------------------------------------------------------------
# RenameCommand
# ---------------------------------------------------------------------------


class TestRenameCommand:
    @pytest.mark.unit
    async def test_rename_sets_name(self, tmp_path):
        ctx = _make_context("rename", args="meu debug")
        with patch.object(ConversationNameStore, "set") as mock_set:
            cmd = RenameCommand()
            result = await cmd.execute(ctx)

        assert result.success
        mock_set.assert_called_once_with(ctx.session.session_id, "meu debug")

    @pytest.mark.unit
    async def test_rename_updates_context_data(self, tmp_path):
        ctx = _make_context("rename", args="novo nome")
        with patch.object(ConversationNameStore, "set"):
            cmd = RenameCommand()
            await cmd.execute(ctx)

        assert ctx.session.context_data.get("conversation_name") == "novo nome"

    @pytest.mark.unit
    async def test_rename_no_name_returns_error(self):
        ctx = _make_context("rename", args="")
        cmd = RenameCommand()
        result = await cmd.execute(ctx)
        assert not result.success

    @pytest.mark.unit
    async def test_rename_no_session_returns_error(self):
        ctx = _make_context("rename", args="test")
        ctx.session = None
        cmd = RenameCommand()
        result = await cmd.execute(ctx)
        assert not result.success


# ---------------------------------------------------------------------------
# RewindCommand
# ---------------------------------------------------------------------------


class TestRewindCommand:
    @pytest.mark.unit
    async def test_rewind_empty_history(self):
        ctx = _make_context("rewind", history=[])
        cmd = RewindCommand()
        result = await cmd.execute(ctx)
        assert result.success
        # No session switch when history is empty
        assert "_switch_session" not in ctx.session.context_data

    @pytest.mark.unit
    async def test_rewind_selector_not_supported(self):
        from deile.commands.builtin import rewind_command as rw_mod

        ctx = _make_context("rewind", history=_HISTORY)
        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = False

        with patch.object(rw_mod, "get_default_selector", return_value=mock_selector):
            cmd = RewindCommand()
            result = await cmd.execute(ctx)

        assert not result.success

    @pytest.mark.unit
    async def test_rewind_cancel_returns_no_switch(self):
        from deile.commands.builtin import rewind_command as rw_mod

        ctx = _make_context("rewind", history=_HISTORY)
        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = True
        mock_selector.select = AsyncMock(return_value=None)

        with patch.object(rw_mod, "get_default_selector", return_value=mock_selector):
            cmd = RewindCommand()
            result = await cmd.execute(ctx)

        assert result.success
        assert result.metadata.get("cancelled")
        assert "_switch_session" not in ctx.session.context_data

    @pytest.mark.unit
    async def test_rewind_choice_creates_fork(self):
        from deile.commands.builtin import rewind_command as rw_mod
        from deile.core.interfaces.selector import SelectorOption

        ctx = _make_context("rewind", history=_HISTORY)
        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = True
        # Select the first user message (index 0 in history)
        mock_selector.select = AsyncMock(
            return_value=SelectorOption(label="#1 olá mundo", value=0)
        )

        with patch.object(rw_mod, "get_default_selector", return_value=mock_selector):
            cmd = RewindCommand()
            result = await cmd.execute(ctx)

        assert result.success
        new_sid = ctx.session.context_data.get("_switch_session")
        assert new_sid is not None
        assert new_sid.startswith("rewind-")

        # Fork should contain only up to the first user message (index 0 + 1 = 1 entry)
        new_sess = ctx.agent.get_session(new_sid)
        assert len(new_sess.conversation_history) == 1
        assert new_sess.conversation_history[0]["content"] == "olá mundo"


# ---------------------------------------------------------------------------
# ResumeCommand
# ---------------------------------------------------------------------------


class TestSessionHistoryStore:
    def test_save_and_list(self, tmp_path):
        store = SessionHistoryStore(base_dir=tmp_path)
        history = [
            {"role": "user", "content": "hello", "timestamp": 1.0, "metadata": {}},
            {"role": "assistant", "content": "hi!", "timestamp": 1.1, "metadata": {}},
        ]
        store.save("s1", history, name="my session")
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"
        assert sessions[0]["first_user_input"] == "hello"
        assert sessions[0]["message_count"] == 2

    def test_list_empty(self, tmp_path):
        store = SessionHistoryStore(base_dir=tmp_path)
        assert store.list_sessions() == []

    def test_load_returns_none_for_missing(self, tmp_path):
        store = SessionHistoryStore(base_dir=tmp_path)
        assert store.load("nonexistent") is None

    def test_load_returns_stored_data(self, tmp_path):
        store = SessionHistoryStore(base_dir=tmp_path)
        history = [{"role": "user", "content": "test", "timestamp": 1.0, "metadata": {}}]
        store.save("s2", history)
        data = store.load("s2")
        assert data is not None
        assert data["session_id"] == "s2"
        assert len(data["history"]) == 1

    def test_slash_command_messages_excluded_from_first_user_input(self, tmp_path):
        store = SessionHistoryStore(base_dir=tmp_path)
        history = [
            {"role": "user", "content": "/fork", "timestamp": 1.0, "metadata": {}},
            {"role": "user", "content": "real question", "timestamp": 2.0, "metadata": {}},
        ]
        store.save("s3", history)
        sessions = store.list_sessions()
        assert sessions[0]["first_user_input"] == "real question"

    def test_corrupted_file_skipped(self, tmp_path):
        session_dir = tmp_path / "bad-session"
        session_dir.mkdir()
        (session_dir / "history.json").write_text("NOT JSON", encoding="utf-8")
        store = SessionHistoryStore(base_dir=tmp_path)
        assert store.list_sessions() == []

    def test_sorted_by_last_activity(self, tmp_path):
        store = SessionHistoryStore(base_dir=tmp_path)
        store.save("older", [{"role": "user", "content": "x", "timestamp": 1.0, "metadata": {}}])
        # Overwrite last_activity by re-saving slightly later
        import time as _time
        _time.sleep(0.01)
        store.save("newer", [{"role": "user", "content": "y", "timestamp": 2.0, "metadata": {}}])
        sessions = store.list_sessions()
        assert sessions[0]["session_id"] == "newer"


class TestResumeCommand:
    _SESSIONS = [
        {
            "session_id": "old-1",
            "conversation_name": "",
            "last_activity": 2.0,
            "first_user_input": "hello",
            "message_count": 4,
        }
    ]
    _STORED = {
        "session_id": "old-1",
        "conversation_name": "",
        "last_activity": 2.0,
        "history": [
            {"role": "user", "content": "hi", "timestamp": 1.0, "metadata": {}},
            {"role": "assistant", "content": "hello", "timestamp": 1.0, "metadata": {}},
            {"role": "user", "content": "bye", "timestamp": 2.0, "metadata": {}},
            {"role": "assistant", "content": "cya", "timestamp": 2.0, "metadata": {}},
        ],
    }

    @pytest.mark.unit
    async def test_resume_no_sessions(self):
        ctx = _make_context("resume")
        with patch.object(SessionHistoryStore, "list_sessions", return_value=[]):
            cmd = ResumeCommand()
            result = await cmd.execute(ctx)
        assert result.success
        assert "_switch_session" not in ctx.session.context_data

    @pytest.mark.unit
    async def test_resume_cancel(self):
        from deile.commands.builtin import resume_command as res_mod

        ctx = _make_context("resume")

        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = True
        mock_selector.select = AsyncMock(return_value=None)

        with (
            patch.object(SessionHistoryStore, "list_sessions", return_value=self._SESSIONS),
            patch.object(res_mod, "get_default_selector", return_value=mock_selector),
            patch.object(ConversationNameStore, "get", return_value=None),
        ):
            cmd = ResumeCommand()
            result = await cmd.execute(ctx)

        assert result.success
        assert result.metadata.get("cancelled")

    @pytest.mark.unit
    async def test_resume_loads_history(self):
        from deile.commands.builtin import resume_command as res_mod
        from deile.core.interfaces.selector import SelectorOption

        ctx = _make_context("resume")

        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = True
        mock_selector.select = AsyncMock(
            return_value=SelectorOption(label="hi", value="old-1")
        )

        with (
            patch.object(SessionHistoryStore, "list_sessions", return_value=self._SESSIONS),
            patch.object(SessionHistoryStore, "load", return_value=self._STORED),
            patch.object(res_mod, "get_default_selector", return_value=mock_selector),
            patch.object(ConversationNameStore, "get", return_value=None),
        ):
            cmd = ResumeCommand()
            result = await cmd.execute(ctx)

        assert result.success
        new_sid = ctx.session.context_data.get("_switch_session")
        # /resume reuses the stored conversation's session_id (not a fork);
        # value matches what the selector mock returned ("old-1").
        assert new_sid == "old-1"
        assert ctx.session.context_data.get("_post_switch_action") == "replay"
        assert (result.metadata or {}).get("suppress_response_display") is True

        new_sess = ctx.agent.get_session(new_sid)
        assert len(new_sess.conversation_history) == 4

    @pytest.mark.unit
    async def test_resume_selector_not_supported(self):
        from deile.commands.builtin import resume_command as res_mod

        ctx = _make_context("resume")

        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = False

        with (
            patch.object(SessionHistoryStore, "list_sessions", return_value=self._SESSIONS),
            patch.object(res_mod, "get_default_selector", return_value=mock_selector),
            patch.object(ConversationNameStore, "get", return_value=None),
        ):
            cmd = ResumeCommand()
            result = await cmd.execute(ctx)

        assert not result.success

    @pytest.mark.unit
    async def test_resume_load_failure(self):
        from deile.commands.builtin import resume_command as res_mod
        from deile.core.interfaces.selector import SelectorOption

        ctx = _make_context("resume")

        mock_selector = MagicMock()
        mock_selector.is_supported.return_value = True
        mock_selector.select = AsyncMock(
            return_value=SelectorOption(label="hi", value="old-1")
        )

        with (
            patch.object(SessionHistoryStore, "list_sessions", return_value=self._SESSIONS),
            patch.object(SessionHistoryStore, "load", return_value=None),
            patch.object(res_mod, "get_default_selector", return_value=mock_selector),
            patch.object(ConversationNameStore, "get", return_value=None),
        ):
            cmd = ResumeCommand()
            result = await cmd.execute(ctx)

        assert not result.success


# ---------------------------------------------------------------------------
# EpisodicMemory — new methods
# ---------------------------------------------------------------------------


class TestEpisodicMemoryNewMethods:
    @pytest.fixture
    async def mem(self, tmp_path):
        em = EpisodicMemory(storage_dir=tmp_path / "episodic")
        await em.initialize()
        return em

    @pytest.mark.unit
    async def test_list_sessions_empty(self, mem):
        sessions = await mem.list_sessions()
        assert sessions == []

    @pytest.mark.unit
    async def test_list_sessions_returns_rows(self, mem):
        await mem.store_episode("hello", "world", session_id="s1")
        await mem.store_episode("bye", "cya", session_id="s2")
        sessions = await mem.list_sessions()
        assert len(sessions) == 2
        sids = {r["session_id"] for r in sessions}
        assert "s1" in sids and "s2" in sids

    @pytest.mark.unit
    async def test_list_sessions_ordered_by_recency(self, mem):
        await mem.store_episode("old", "resp", session_id="early")
        await mem.store_episode("new", "resp", session_id="recent")
        sessions = await mem.list_sessions()
        assert sessions[0]["session_id"] == "recent"

    @pytest.mark.unit
    async def test_get_episodes_for_session(self, mem):
        await mem.store_episode("q1", "a1", session_id="sx")
        await mem.store_episode("q2", "a2", session_id="sx")
        eps = await mem.get_episodes_for_session("sx")
        assert len(eps) == 2
        assert eps[0]["user_input"] == "q1"
        assert eps[1]["user_input"] == "q2"

    @pytest.mark.unit
    async def test_get_episodes_for_unknown_session(self, mem):
        eps = await mem.get_episodes_for_session("nonexistent")
        assert eps == []

    @pytest.mark.unit
    async def test_list_sessions_respects_max(self, mem):
        for i in range(10):
            await mem.store_episode(f"q{i}", f"a{i}", session_id=f"s{i}")
        sessions = await mem.list_sessions(max_sessions=5)
        assert len(sessions) == 5
