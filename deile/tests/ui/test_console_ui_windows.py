"""Tests for ``ConsoleUIManager`` legacy-Windows construction logic.

``ConsoleUIManager.__init__`` constructs a Rich ``Console`` with
``legacy_windows=True`` when **all** of these hold:

* ``os.name == 'nt'`` (running on Windows)
* No ``WT_SESSION`` (i.e. NOT Windows Terminal)
* No ``ANSICON``
* No ``ConEmuPID``
* ``TERM`` is unset, empty, or literally ``'cygwin'``

When any of those env hints is present (modern Windows terminals OR any
POSIX system), Rich's auto-detection takes over and ``legacy_windows`` is
left at its default ``False``. Forcing the legacy path on macOS / Linux
breaks ``Live`` Markdown rendering (see the inline comment in
``ConsoleUIManager.__init__``), so this branch deserves explicit coverage.

The tests patch the ``Console`` symbol inside ``deile.ui.console_ui`` to
inspect the keyword arguments the constructor would have received.
"""

from __future__ import annotations

import os
from typing import Dict, Optional
from unittest.mock import patch

import pytest

_LEGACY_HINT_KEYS = ("WT_SESSION", "ANSICON", "ConEmuPID", "TERM")


def _purge_legacy_hints() -> Dict[str, Optional[str]]:
    """Strip env hints that would push Rich off the legacy-Windows path.

    Returns the snapshot so callers can restore it in a ``finally``."""
    saved = {k: os.environ.get(k) for k in _LEGACY_HINT_KEYS}
    for k in _LEGACY_HINT_KEYS:
        os.environ.pop(k, None)
    return saved


def _restore_env(saved: Dict[str, Optional[str]]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.mark.ui
@pytest.mark.unit
class TestConsoleUIManagerLegacyWindows:
    """``legacy_windows=True`` is forced only when on Windows AND no modern
    terminal hint is present."""

    def test_legacy_windows_true_when_no_modern_hint(self) -> None:
        saved = _purge_legacy_hints()
        try:
            with patch("os.name", "nt"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            mock_console.assert_called_once()
            kwargs = mock_console.call_args.kwargs
            assert kwargs.get("legacy_windows") is True
            assert kwargs.get("force_terminal") is True
            assert kwargs.get("_environ") == {"TERM": "ansi"}
        finally:
            _restore_env(saved)

    def test_legacy_windows_false_when_wt_session_set(self) -> None:
        """Windows Terminal user → modern path, legacy_windows NOT set."""
        saved = _purge_legacy_hints()
        try:
            os.environ["WT_SESSION"] = "any-value"
            with patch("os.name", "nt"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            kwargs = mock_console.call_args.kwargs
            # Either omitted entirely or explicitly False — both indicate
            # the modern path.
            assert kwargs.get("legacy_windows") is not True
        finally:
            _restore_env(saved)

    def test_legacy_windows_false_when_ansicon_set(self) -> None:
        """ANSICON shim user → modern path."""
        saved = _purge_legacy_hints()
        try:
            os.environ["ANSICON"] = "1"
            with patch("os.name", "nt"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            kwargs = mock_console.call_args.kwargs
            assert kwargs.get("legacy_windows") is not True
        finally:
            _restore_env(saved)

    def test_legacy_windows_false_when_conemu_set(self) -> None:
        """ConEmu user → modern path."""
        saved = _purge_legacy_hints()
        try:
            os.environ["ConEmuPID"] = "1234"
            with patch("os.name", "nt"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            kwargs = mock_console.call_args.kwargs
            assert kwargs.get("legacy_windows") is not True
        finally:
            _restore_env(saved)

    def test_legacy_windows_false_when_term_is_set(self) -> None:
        """Any TERM value other than '', None, or 'cygwin' disables legacy."""
        saved = _purge_legacy_hints()
        try:
            os.environ["TERM"] = "xterm-256color"
            with patch("os.name", "nt"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            kwargs = mock_console.call_args.kwargs
            assert kwargs.get("legacy_windows") is not True
        finally:
            _restore_env(saved)

    def test_legacy_windows_true_when_term_is_cygwin(self) -> None:
        """``TERM=cygwin`` is treated like 'no modern terminal'."""
        saved = _purge_legacy_hints()
        try:
            os.environ["TERM"] = "cygwin"
            with patch("os.name", "nt"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            kwargs = mock_console.call_args.kwargs
            assert kwargs.get("legacy_windows") is True
        finally:
            _restore_env(saved)

    def test_legacy_windows_false_on_posix(self) -> None:
        """Negative control — POSIX never takes the legacy branch."""
        saved = _purge_legacy_hints()
        try:
            with patch("os.name", "posix"), \
                 patch("deile.ui.console_ui.Console") as mock_console:
                from deile.ui.console_ui import ConsoleUIManager
                ConsoleUIManager()

            kwargs = mock_console.call_args.kwargs
            assert kwargs.get("legacy_windows") is not True
        finally:
            _restore_env(saved)
