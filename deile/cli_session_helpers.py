"""Pure helpers for the interactive CLI's history/session bookkeeping.

Extracted from ``_DeileCLI`` so the history-management logic can be unit-tested
without instantiating the full REPL. The class keeps thin wrappers that delegate
here — these functions take explicit dependencies (session, agent, ui) so they
have no implicit coupling to the CLI instance.
"""

from __future__ import annotations

from typing import Any, Optional

from deile.commands._sentinels import POST_SWITCH_ACTION_KEY, SWITCH_SESSION_KEY


def persist_session(session: Any, user_input: str) -> None:
    """Write current session history to disk so /resume can find it.

    Only persists after real LLM turns (not slash commands) to avoid storing
    noise. Failures are silently ignored — non-fatal.
    """
    if user_input.startswith("/"):
        return
    history = getattr(session, "conversation_history", [])
    if not history:
        return
    try:
        from .commands.builtin._session_store import SessionHistoryStore
        name = session.context_data.get("conversation_name", "")
        SessionHistoryStore().save(session.session_id, list(history), name)
    except Exception:
        pass


def rollback_history(session: Any, baseline_len: int) -> None:
    """Trim ``conversation_history`` back to ``baseline_len``.

    Required after ESC/Ctrl+C: providers (DeepSeek, OpenAI) collapse two
    consecutive ``user`` turns with ``/`` as a separator, so leaving an orphan
    user entry poisons the next reply.
    """
    history = getattr(session, "conversation_history", None)
    if history is None:
        return
    if len(history) > baseline_len:
        del history[baseline_len:]


def replay_history(ui: Any, session: Any, history: list) -> None:
    """Re-render a loaded conversation as if it had just happened.

    Re-uses ``ui.show_welcome`` (which clears the screen) so the replay always
    starts from a fresh canvas, then iterates the stored ``conversation_history``
    rendering each ``user`` entry with the same prompt prefix the live loop uses
    (``\\n > <text>``) and each ``assistant`` entry through ``ui.display_response``
    (which prints the ``Deile >`` header). Non-string contents are normalized via
    ``_normalize_history_content`` to handle any Rich renderable that slipped
    through.
    """
    from .core.agent import _normalize_history_content
    ui.show_welcome(session)
    for entry in history:
        role = entry.get("role")
        raw = entry.get("content")
        text = _normalize_history_content(raw)
        if not text:
            continue
        if role == "user":
            ui.console.print(f"\n > {text}")
        elif role == "assistant":
            ui.display_response(text, metadata=None)


def check_session_switch(session: Any, agent: Any, ui: Any) -> Optional[Any]:
    """Apply a pending session switch and return the new session, or ``None``.

    ``/fork``, ``/rewind`` and ``/resume`` request a switch by writing
    ``SWITCH_SESSION_KEY`` (and optionally ``POST_SWITCH_ACTION_KEY``) into
    ``session.context_data``. This pops both keys, resolves the new session via
    ``agent.get_session``, runs the requested follow-up UI work, and returns the
    new session for the caller to assign. Returns ``None`` if no switch was
    requested or if the target session was not found.
    """
    ctx = session.context_data
    new_sid = ctx.pop(SWITCH_SESSION_KEY, None)
    action = ctx.pop(POST_SWITCH_ACTION_KEY, None)
    if not new_sid:
        return None
    new_session = agent.get_session(new_sid)
    if new_session is None:
        ui.console.print(f"[yellow]Sessão {new_sid!r} não encontrada — mantendo atual.[/yellow]")
        return None

    if action == "welcome":
        ui.show_welcome(new_session)
    elif action == "replay":
        replay_history(ui, new_session, list(new_session.conversation_history or []))
    else:
        name = new_session.context_data.get("conversation_name", "")
        label = f'"{name}"' if name else new_sid
        ui.console.print(f"\n[dim cyan]› Sessão alternada para {label}[/dim cyan]")
    return new_session
