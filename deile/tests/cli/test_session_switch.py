"""Tests for `_DeileCLI._check_session_switch` post-switch actions.

The CLI looks for ``_switch_session`` and ``_post_switch_action`` keys in
the current session's ``context_data`` after each turn. ``"welcome"``
redraws the entry banner; ``"replay"`` clears the screen and re-renders
every entry of the loaded conversation; absence falls back to the dim
``"› Sessão alternada para …"`` line.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from deile.cli import _DeileCLI


def _make_session(
    sid: str = "sid",
    history: Optional[List[Dict[str, Any]]] = None,
    context_data: Optional[Dict[str, Any]] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=sid,
        conversation_history=list(history or []),
        context_data=dict(context_data or {}),
    )


def _make_cli(current_session, target_session=None) -> _DeileCLI:
    cli = _DeileCLI()
    cli.default_session = current_session
    agent = MagicMock()
    agent.get_session.side_effect = lambda sid: (
        target_session if target_session and sid == target_session.session_id else None
    )
    cli.agent = agent
    cli.ui = MagicMock()
    return cli


class TestCheckSessionSwitch:
    def test_no_switch_when_sentinel_absent(self):
        sess = _make_session()
        cli = _make_cli(sess)
        cli._check_session_switch()
        assert cli.default_session is sess
        cli.ui.show_welcome.assert_not_called()

    def test_welcome_action_redraws_banner(self):
        target = _make_session(sid="new-sid")
        current = _make_session(
            context_data={
                "_switch_session": "new-sid",
                "_post_switch_action": "welcome",
            },
        )
        cli = _make_cli(current, target_session=target)
        cli._check_session_switch()
        assert cli.default_session is target
        cli.ui.show_welcome.assert_called_once_with()

    def test_replay_action_invokes_replay(self):
        target = _make_session(
            sid="resumed-sid",
            history=[
                {"role": "user", "content": "hi", "timestamp": 1.0, "metadata": {}},
                {"role": "assistant", "content": "hello", "timestamp": 1.1, "metadata": {}},
            ],
        )
        current = _make_session(
            context_data={
                "_switch_session": "resumed-sid",
                "_post_switch_action": "replay",
            },
        )
        cli = _make_cli(current, target_session=target)
        cli._check_session_switch()
        assert cli.default_session is target
        # Replay redraws via show_welcome + display_response.
        cli.ui.show_welcome.assert_called_once_with()
        cli.ui.display_response.assert_called_once()

    def test_default_action_prints_dim_swap_line(self):
        target = _make_session(sid="new-sid")
        current = _make_session(
            context_data={"_switch_session": "new-sid"},
        )
        cli = _make_cli(current, target_session=target)
        cli._check_session_switch()
        assert cli.default_session is target
        cli.ui.show_welcome.assert_not_called()
        cli.ui.console.print.assert_called_once()
        text = cli.ui.console.print.call_args.args[0]
        assert "Sessão alternada" in text

    def test_unknown_target_keeps_current_session(self):
        sess = _make_session(context_data={"_switch_session": "missing-sid"})
        cli = _make_cli(sess, target_session=None)
        cli._check_session_switch()
        assert cli.default_session is sess
        cli.ui.console.print.assert_called_once()

    def test_sentinels_are_consumed_even_when_target_missing(self):
        sess = _make_session(context_data={
            "_switch_session": "missing-sid",
            "_post_switch_action": "welcome",
        })
        cli = _make_cli(sess, target_session=None)
        cli._check_session_switch()
        assert "_switch_session" not in sess.context_data
        assert "_post_switch_action" not in sess.context_data


class TestReplayHistory:
    def test_replay_renders_user_and_assistant_entries_in_order(self):
        sess = _make_session()
        cli = _make_cli(sess)
        history = [
            {"role": "user", "content": "primeiro", "timestamp": 1.0},
            {"role": "assistant", "content": "resp 1", "timestamp": 1.1},
            {"role": "user", "content": "segundo", "timestamp": 2.0},
            {"role": "assistant", "content": "resp 2", "timestamp": 2.1},
        ]
        cli._replay_history(history)
        cli.ui.show_welcome.assert_called_once_with()
        # Two assistant entries → two display_response calls.
        assert cli.ui.display_response.call_count == 2
        # Two user entries → two console.print calls with the "> " prefix.
        user_prints = [
            c for c in cli.ui.console.print.call_args_list
            if c.args and isinstance(c.args[0], str) and c.args[0].startswith("\n > ")
        ]
        assert len(user_prints) == 2
        assert "primeiro" in user_prints[0].args[0]
        assert "segundo" in user_prints[1].args[0]

    def test_replay_skips_empty_entries(self):
        sess = _make_session()
        cli = _make_cli(sess)
        history = [
            {"role": "user", "content": "ok", "timestamp": 1.0},
            {"role": "assistant", "content": "", "timestamp": 1.1},
            {"role": "user", "content": None, "timestamp": 2.0},
        ]
        cli._replay_history(history)
        # Only the non-empty user entry rendered.
        user_prints = [
            c for c in cli.ui.console.print.call_args_list
            if c.args and isinstance(c.args[0], str) and c.args[0].startswith("\n > ")
        ]
        assert len(user_prints) == 1
        cli.ui.display_response.assert_not_called()

    def test_replay_normalizes_non_string_content(self):
        """Non-string content (Rich renderable) must be coerced to text."""
        sess = _make_session()
        cli = _make_cli(sess)
        from rich.panel import Panel
        history = [
            {"role": "assistant", "content": Panel("rendered"), "timestamp": 1.0},
        ]
        cli._replay_history(history)
        cli.ui.display_response.assert_called_once()
        rendered = cli.ui.display_response.call_args.args[0]
        assert isinstance(rendered, str)
        assert "rendered" in rendered

    def test_replay_with_empty_history_only_shows_welcome(self):
        sess = _make_session()
        cli = _make_cli(sess)
        cli._replay_history([])
        cli.ui.show_welcome.assert_called_once_with()
        cli.ui.display_response.assert_not_called()
