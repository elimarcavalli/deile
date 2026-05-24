"""Stdin ownership coordination + termios safety-net for the subagent panel.

The CLI's ESC watcher and the panel's keyboard watcher both read ``stdin``.
``read()`` is exclusive, so without coordination the CLI eats bytes that
should reach the panel. The panel ``claim`` s stdin (the CLI pauses while
the flag is set) and ``release`` s it on exit.

The panel watcher runs in a daemon thread — if the process dies abruptly
(Ctrl+C, unhandled exception), its ``finally`` does not run and the
terminal stays in cbreak. We capture the original (cooked) termios on the
first claim and register an ``atexit`` handler that restores it; ``atexit``
runs on the main thread even when daemons are killed.
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading

logger = logging.getLogger(__name__)

_panel_owns_stdin = threading.Event()
_saved_termios = None
_termios_fd: int = -1
_atexit_registered = False
_lock = threading.Lock()


def _restore_termios() -> None:
    """Idempotent restore of the captured cooked termios. Never raises."""
    global _saved_termios, _termios_fd
    if _saved_termios is None:
        return
    try:
        import termios
        if _termios_fd >= 0:
            termios.tcsetattr(_termios_fd, termios.TCSADRAIN, _saved_termios)
    except Exception:
        try:
            logger.debug("termios restore failed", exc_info=True)
        except Exception:
            pass
    finally:
        _saved_termios = None
        _termios_fd = -1


def prime_termios_snapshot(original_termios=None) -> None:
    """Record the *original cooked* termios for the atexit safety-net.

    Caller (CLI) should pass the snapshot captured BEFORE its own ``setcbreak``;
    when ``None`` we auto-capture the current state but refuse it if the
    terminal is already in cbreak (capturing cbreak as "original" would let
    atexit leave the terminal broken). Idempotent — first snapshot wins.
    """
    global _saved_termios, _termios_fd, _atexit_registered

    with _lock:
        if _saved_termios is None and sys.stdin.isatty():
            try:
                import termios
                fd = sys.stdin.fileno()
                if original_termios is not None:
                    _saved_termios = original_termios
                    _termios_fd = fd
                else:
                    current = termios.tcgetattr(fd)
                    # lflag (index 3) carries ICANON. ICANON off ⇒ cbreak.
                    lflag = current[3] if len(current) > 3 else 0
                    if not (lflag & termios.ICANON):
                        logger.warning(
                            "prime_termios_snapshot: terminal already in cbreak; "
                            "ignoring auto-capture. Pass the cooked snapshot "
                            "explicitly via original_termios=."
                        )
                    else:
                        _saved_termios = current
                        _termios_fd = fd
            except Exception:
                logger.debug("could not capture termios snapshot", exc_info=True)

        if not _atexit_registered:
            try:
                atexit.register(_restore_termios)
                _atexit_registered = True
            except Exception:
                pass


def claim_stdin_for_panel(original_termios=None) -> None:
    """Panel announces exclusive stdin reading.

    Also flushes the stdin input buffer (TCIFLUSH) to drop bytes the CLI
    watcher may have queued before the claim took effect — closes the
    TOCTOU window between user keypress and the claim.
    """
    prime_termios_snapshot(original_termios=original_termios)
    _panel_owns_stdin.set()

    if sys.stdin.isatty():
        try:
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass


def release_stdin_for_panel() -> None:
    """Panel returns stdin to the CLI. Does NOT restore termios — the CLI
    owns its own cbreak/restore cycle; the atexit handler is the safety net.
    """
    _panel_owns_stdin.clear()


def panel_owns_stdin() -> bool:
    return _panel_owns_stdin.is_set()


def restore_termios_now() -> None:
    """Manual restore for SIGINT handlers / tests."""
    _restore_termios()


__all__ = [
    "claim_stdin_for_panel",
    "panel_owns_stdin",
    "prime_termios_snapshot",
    "release_stdin_for_panel",
    "restore_termios_now",
]
