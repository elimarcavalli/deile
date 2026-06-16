"""Tests for Windows-specific branches in ``deile/ui/emoji_support.py``.

Two functions take a ``sys.platform == "win32"`` branch to switch the
console code page to UTF-8 via ``chcp 65001`` so emojis render correctly
in ``cmd.exe`` / ``conhost.exe``:

* ``EmojiManager._check_emoji_support`` — invoked once during init.
* ``EmojiManager.enable_unicode_console`` — public helper that callers
  can invoke later (e.g. after the UI activates).

Both are tested via mocked ``sys.platform`` + mocked ``subprocess.run``
so the Linux CI never actually shells out to ``cmd``.
"""

from __future__ import annotations

import os
from typing import Dict, Optional
from unittest.mock import patch

import pytest


def _purge_emoji_env() -> Dict[str, Optional[str]]:
    """Remove env vars that would short-circuit ``_check_emoji_support``.

    The function returns ``True`` immediately if any of these is set, so
    leaving them in the test environment makes the chcp branch unreachable.
    Caller is responsible for restoring values via the returned dict.
    """
    keys = ("TERM_PROGRAM", "COLORTERM", "WT_SESSION")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
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
class TestCheckEmojiSupportWindows:
    """``_check_emoji_support`` calls ``chcp 65001`` when ``sys.platform == 'win32'``."""

    def test_runs_chcp_and_returns_true_on_win32(self) -> None:
        saved = _purge_emoji_env()
        try:
            with (
                patch("deile.ui.emoji_support.sys.platform", "win32"),
                patch("deile.ui.emoji_support.subprocess.run") as mock_run,
            ):
                from deile.ui.emoji_support import EmojiManager

                manager = EmojiManager()

            assert manager.supports_emoji is True
            mock_run.assert_called_once()
            assert mock_run.call_args[0][0] == ["cmd", "/c", "chcp", "65001"]
            # capture_output suppresses any leakage into the test stdout.
            assert mock_run.call_args.kwargs.get("capture_output") is True
            # check=False so a missing/erroring chcp never crashes init.
            assert mock_run.call_args.kwargs.get("check") is False
        finally:
            _restore_env(saved)

    def test_returns_false_when_chcp_raises_on_win32(self) -> None:
        """Bare exception from subprocess.run on Windows → supports_emoji=False.

        Production code wraps ``subprocess.run`` in a try/except that
        swallows the exception and returns ``False``; nothing must escape.
        """
        saved = _purge_emoji_env()
        try:
            with (
                patch("deile.ui.emoji_support.sys.platform", "win32"),
                patch(
                    "deile.ui.emoji_support.subprocess.run",
                    side_effect=OSError("chcp missing"),
                ),
            ):
                from deile.ui.emoji_support import EmojiManager

                manager = EmojiManager()

            assert manager.supports_emoji is False
        finally:
            _restore_env(saved)


@pytest.mark.ui
@pytest.mark.unit
class TestEnableUnicodeConsoleWindows:
    """``enable_unicode_console`` is a public no-op on non-Windows, and
    runs ``chcp 65001`` + sets ``PYTHONIOENCODING=utf-8`` on Windows."""

    def test_runs_chcp_and_sets_pythonioencoding_on_win32(self) -> None:
        # Snapshot env so the test cleanup is deterministic.
        saved_env = os.environ.get("PYTHONIOENCODING")
        try:
            with (
                patch("deile.ui.emoji_support.sys.platform", "win32"),
                patch("deile.ui.emoji_support.subprocess.run") as mock_run,
            ):
                from deile.ui.emoji_support import EmojiManager

                # Bypass __init__ — we don't want the init-time chcp call to
                # pollute the call count. ``object.__new__`` skips `__init__`
                # entirely and we set the fields the method needs directly.
                manager = EmojiManager.__new__(EmojiManager)
                manager.supports_emoji = True
                manager.emoji_map = {}

                result = manager.enable_unicode_console()

            assert result is True
            mock_run.assert_called_once()
            assert mock_run.call_args[0][0] == ["cmd", "/c", "chcp", "65001"]
            assert os.environ.get("PYTHONIOENCODING") == "utf-8"
        finally:
            if saved_env is None:
                os.environ.pop("PYTHONIOENCODING", None)
            else:
                os.environ["PYTHONIOENCODING"] = saved_env

    def test_returns_true_without_chcp_on_posix(self) -> None:
        """Negative control — POSIX path is a no-op that still returns True."""
        with (
            patch("deile.ui.emoji_support.sys.platform", "linux"),
            patch("deile.ui.emoji_support.subprocess.run") as mock_run,
        ):
            from deile.ui.emoji_support import EmojiManager

            manager = EmojiManager.__new__(EmojiManager)
            manager.supports_emoji = True
            manager.emoji_map = {}

            result = manager.enable_unicode_console()

        assert result is True
        # chcp must NOT be invoked outside Windows.
        mock_run.assert_not_called()

    def test_returns_false_when_chcp_raises_on_win32(self) -> None:
        """Exception in subprocess.run → enable_unicode_console returns False
        and does NOT raise. The fallback is degraded emoji rendering, not a
        crash on Windows users' first session."""
        with (
            patch("deile.ui.emoji_support.sys.platform", "win32"),
            patch(
                "deile.ui.emoji_support.subprocess.run",
                side_effect=OSError("chcp missing"),
            ),
        ):
            from deile.ui.emoji_support import EmojiManager

            manager = EmojiManager.__new__(EmojiManager)
            manager.supports_emoji = True
            manager.emoji_map = {}

            result = manager.enable_unicode_console()

        assert result is False
