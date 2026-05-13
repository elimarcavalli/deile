"""Regression test for ESC-cancel rollback of conversation history.

Before this fix, pressing ESC during a streaming turn would cancel the
asyncio task but leave the user message that ``DeileAgent.process_input_stream``
had already appended to ``session.conversation_history``. The orphan user
entry poisoned the next turn — providers (DeepSeek/OpenAI) collapse two
consecutive ``user`` messages with ``/`` as a separator, so the next turn
would echo the cancelled message in the assistant's reply (manifested as
``"o que eu pedi / o que acabei de dizer?"``).

This test exercises ``_DeileCLI._rollback_history`` directly with a fake
session, since spinning up the full agent + a real ESC keypress is not
feasible in pytest.
"""

from __future__ import annotations

from types import SimpleNamespace

from deile.cli import _DeileCLI


def _make_cli(history: list) -> _DeileCLI:
    cli = _DeileCLI()
    cli.default_session = SimpleNamespace(conversation_history=history)
    return cli


def test_rollback_removes_orphan_user_entry() -> None:
    history = [
        {"role": "user", "content": "earlier turn", "timestamp": 0.0, "metadata": {}},
        {"role": "assistant", "content": "earlier reply", "timestamp": 0.1, "metadata": {}},
        {"role": "user", "content": "cancelled message", "timestamp": 0.2, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=2)
    assert len(history) == 2
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "earlier reply"


def test_rollback_removes_orphan_user_plus_partial_assistant() -> None:
    """Cancellation during the assistant generation must also clear any
    partial assistant entry the agent wrote before the cancel point."""
    history = [
        {"role": "user", "content": "earlier turn", "timestamp": 0.0, "metadata": {}},
        {"role": "assistant", "content": "earlier reply", "timestamp": 0.1, "metadata": {}},
        {"role": "user", "content": "cancelled message", "timestamp": 0.2, "metadata": {}},
        {"role": "assistant", "content": "half of a reply", "timestamp": 0.3, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=2)
    assert len(history) == 2
    assert history[-1]["content"] == "earlier reply"


def test_rollback_noop_when_history_already_at_baseline() -> None:
    """If the turn completed cleanly (no cancel), the baseline-length call
    on the *unchanged* post-turn length should not delete anything."""
    history = [
        {"role": "user", "content": "u1", "timestamp": 0.0, "metadata": {}},
        {"role": "assistant", "content": "a1", "timestamp": 0.1, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=2)
    assert len(history) == 2


def test_rollback_handles_session_without_history_attribute() -> None:
    """Must not crash if the session somehow lacks ``conversation_history``."""
    cli = _DeileCLI()
    cli.default_session = SimpleNamespace()  # no conversation_history
    cli._rollback_history(baseline_len=0)  # should silently no-op


def test_rollback_from_empty_baseline() -> None:
    """First-ever turn cancelled: baseline_len=0, history had one entry
    appended by the agent, rollback must clear everything."""
    history = [
        {"role": "user", "content": "first ever", "timestamp": 0.0, "metadata": {}},
    ]
    cli = _make_cli(history)
    cli._rollback_history(baseline_len=0)
    assert history == []
