"""Tests for the Windows-specific branches of the CLI layer.

Two production paths are covered:

* ``deile/cli.py::_stream_with_esc_cancel`` — Unix-only ``termios``/``tty``
  imports inside a ``try/except ImportError`` block fall back to the plain
  ``display_streaming_turn`` on Windows. Without this, the ESC-cancel
  feature would crash the agent on the first message Windows users send.
* ``deile/ui/cli.py::CLI.clear_screen`` — uses ``cmd /c cls`` when
  ``os.name == 'nt'`` and ``clear`` elsewhere.

Both paths run the Windows logic on the Linux CI runner via mocks; no
Windows-only test environment is required.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# _stream_with_esc_cancel — termios fallback
# ─────────────────────────────────────────────────────────────────────────────


async def _empty_stream() -> AsyncIterator:
    """Tiny async iterator so the test exercises the function signature
    without depending on real model events."""
    return
    yield  # noqa: unreachable — needed to make this an async generator


@pytest.mark.unit
class TestStreamWithEscCancelTermiosFallback:
    """When ``termios`` cannot be imported (the Windows scenario), the
    function must fall back to plain ``display_streaming_turn`` and return
    ``False`` (no cancellation) — never raise.

    Implementation note: we patch ``builtins.__import__`` to selectively
    raise ``ImportError`` for the Unix-only names. Mutating
    ``sys.modules`` directly (the more common idiom) ended up polluting
    pytest's ``caplog`` machinery for subsequent tests — patching
    ``__import__`` is fully scoped to the ``with`` block and leaves
    global state untouched.
    """

    def test_falls_back_when_termios_absent(self) -> None:
        from deile.cli import _DeileCLI

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def _selective_import(name, *args, **kwargs):
            # Make the lazy `import termios` / `import tty` inside
            # _stream_with_esc_cancel behave like they do on Windows.
            if name in ("termios", "tty"):
                raise ImportError(f"No module named {name!r} (simulated Windows)")
            return real_import(name, *args, **kwargs)

        cli = _DeileCLI.__new__(_DeileCLI)
        cli.ui = MagicMock()
        cli.ui.display_streaming_turn = AsyncMock(return_value=None)

        with patch("builtins.__import__", side_effect=_selective_import):
            result = asyncio.run(
                cli._stream_with_esc_cancel(_empty_stream())
            )

        # Function returned False (no cancellation) because the
        # termios import raised ImportError — exactly the Windows
        # behavior we want to preserve.
        assert result is False
        # The plain renderer was invoked instead of the cbreak watcher.
        cli.ui.display_streaming_turn.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# ui/cli.py::CLI.clear_screen — Windows uses `cmd /c cls`
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.ui
@pytest.mark.unit
class TestClearScreenWindows:
    """``CLI.clear_screen`` selects ``cmd /c cls`` on Windows.

    The function is a one-liner ``if os.name == 'nt'`` branch — the
    risk is a copy-paste regression silently flipping the operator,
    making ``clear`` run on Windows (where the command doesn't exist
    and the screen never clears).

    Implementation notes:

    * We avoid calling ``CLI()`` directly because ``__init__`` invokes
      ``get_logger()``, which sets ``propagate=False`` on a named logger.
      That mutation is process-global and breaks pytest's ``caplog``
      fixture in any subsequent test that captures from that logger
      family. ``__new__`` skips ``__init__`` entirely so the bound
      ``clear_screen`` method runs without poisoning logging.
    * ``patch("deile.ui.cli.os.name", "nt")`` mutates the real
      ``os.name``, which would also make any inline ``Path()`` switch to
      ``WindowsPath`` (and crash on POSIX). ``clear_screen`` constructs
      no paths, so the patch is safe here.
    """

    def _make_cli(self):
        """Build a CLI instance without running ``__init__``."""
        from deile.ui.cli import CLI
        return CLI.__new__(CLI)

    def test_uses_cmd_cls_on_windows(self) -> None:
        cli = self._make_cli()
        with patch("deile.ui.cli.os.name", "nt"), \
             patch("deile.ui.cli.subprocess.run") as mock_run:
            cli.clear_screen()

        mock_run.assert_called_once()
        called_args = mock_run.call_args[0][0]
        assert called_args == ["cmd", "/c", "cls"]
        # `check=False` so the call never raises — verify the kwarg.
        assert mock_run.call_args.kwargs.get("check") is False

    def test_uses_clear_on_posix_for_contrast(self) -> None:
        """Negative control — make sure the Windows assertion isn't vacuously
        true."""
        cli = self._make_cli()
        with patch("deile.ui.cli.os.name", "posix"), \
             patch("deile.ui.cli.subprocess.run") as mock_run:
            cli.clear_screen()

        called_args = mock_run.call_args[0][0]
        assert called_args == ["clear"]
