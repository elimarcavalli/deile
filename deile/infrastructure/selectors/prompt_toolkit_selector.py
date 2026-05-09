"""Prompt-toolkit adapter for :class:`InteractiveSelector`.

Renders ``options`` as a vertical list with arrow-key navigation, Enter to
confirm, ESC to cancel, and incremental substring filtering as the user types.
Single-select only.
"""

from __future__ import annotations

import sys
from typing import List, Optional, Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ...core.interfaces.selector import (InteractiveSelector,
                                         SelectorNotSupported, SelectorOption)

_STYLE = Style.from_dict(
    {
        "prompt": "bold cyan",
        "filter": "italic",
        "filter.label": "dim",
        "row.selected": "reverse bold",
        "row": "",
        "row.description": "dim",
        "footer": "dim",
        "empty": "italic #888888",
    }
)


class PromptToolkitSelector(InteractiveSelector):
    """Default :class:`InteractiveSelector` implementation backed by prompt_toolkit."""

    def is_supported(self) -> bool:
        # Dual check: the Python wrapper (mockable in tests) AND the raw fd
        # (survives prompt_toolkit Application teardown on macOS/Linux).
        # If either says TTY, we trust it — false negatives are worse than
        # false positives here since select() has its own guard.
        try:
            import os as _os
            tty_stdin = sys.stdin.isatty() or _os.isatty(sys.stdin.fileno())
            tty_stdout = sys.stdout.isatty() or _os.isatty(sys.stdout.fileno())
            return bool(tty_stdin and tty_stdout)
        except (AttributeError, ValueError, OSError):
            return False

    async def select(
        self,
        options: Sequence[SelectorOption],
        *,
        prompt: str = "Select an option",
        default_index: int = 0,
    ) -> Optional[SelectorOption]:
        if not options:
            raise ValueError("PromptToolkitSelector.select: options must not be empty")

        if not self.is_supported():
            raise SelectorNotSupported(
                "Interactive selector requires a TTY for stdin and stdout"
            )

        opts = list(options)
        state = _SelectorState(
            options=opts,
            cursor=max(0, min(default_index, len(opts) - 1)),
        )

        kb = self._build_key_bindings(state)
        body = FormattedTextControl(lambda: self._render(prompt, state), focusable=True)
        footer = FormattedTextControl(lambda: self._render_footer(state))
        layout = Layout(
            HSplit(
                [
                    Window(content=body, dont_extend_height=True),
                    Window(content=footer, height=1, dont_extend_height=True),
                ]
            )
        )

        app: Application = Application(
            layout=layout,
            key_bindings=kb,
            style=_STYLE,
            full_screen=False,
            mouse_support=False,
            erase_when_done=True,
        )
        state.app = app

        try:
            try:
                result = await app.run_async()
            except (OSError, RuntimeError) as exc:
                # Legacy Windows console (cmd.exe without VT) and other
                # non-interactive terminals raise here even after isatty()
                # said yes. Map to the documented contract so consumers'
                # SelectorNotSupported handler still fires.
                raise SelectorNotSupported(
                    f"prompt_toolkit could not initialise the picker: {exc}"
                ) from exc
        finally:
            state.app = None
        return result if isinstance(result, SelectorOption) else None

    @staticmethod
    def _build_key_bindings(state: "_SelectorState") -> KeyBindings:
        kb = KeyBindings()

        @kb.add("up")
        def _(event):
            state.move(-1)
            event.app.invalidate()

        @kb.add("down")
        def _(event):
            state.move(1)
            event.app.invalidate()

        @kb.add("pageup")
        def _(event):
            state.move(-5)
            event.app.invalidate()

        @kb.add("pagedown")
        def _(event):
            state.move(5)
            event.app.invalidate()

        @kb.add("home")
        def _(event):
            state.set_cursor(0)
            event.app.invalidate()

        @kb.add("end")
        def _(event):
            state.set_cursor(len(state.visible) - 1)
            event.app.invalidate()

        @kb.add("enter")
        def _(event):
            chosen = state.current()
            if chosen is not None:
                event.app.exit(result=chosen)

        @kb.add("escape", eager=True)
        def _(event):
            event.app.exit(result=None)

        # Ctrl+C cancels the picker (treated as ESC) instead of propagating
        # KeyboardInterrupt to the parent CLI — the user almost always means
        # "back out of this menu", not "kill the agent".
        @kb.add("c-c")
        def _(event):
            event.app.exit(result=None)

        @kb.add("backspace")
        def _(event):
            if state.filter_text:
                state.set_filter(state.filter_text[:-1])
                event.app.invalidate()

        @kb.add("<any>")
        def _(event):
            data = event.data
            if data and data.isprintable() and len(data) == 1:
                state.set_filter(state.filter_text + data)
                event.app.invalidate()

        return kb

    @staticmethod
    def _render(prompt: str, state: "_SelectorState") -> FormattedText:
        rows: List = [("class:prompt", f"{prompt}\n")]
        if state.filter_text:
            rows.append(("class:filter.label", "filter: "))
            rows.append(("class:filter", f"{state.filter_text}\n"))
        else:
            rows.append(("class:filter.label", "type to filter\n"))

        if not state.visible:
            rows.append(("class:empty", "  (no matches)\n"))
            return FormattedText(rows)

        for idx, opt in enumerate(state.visible):
            style = "class:row.selected" if idx == state.cursor else "class:row"
            marker = "▶ " if idx == state.cursor else "  "
            rows.append((style, f"{marker}{opt.label}"))
            if opt.description:
                rows.append(("class:row.description", f"  — {opt.description}"))
            rows.append(("", "\n"))
        return FormattedText(rows)

    @staticmethod
    def _render_footer(state: "_SelectorState") -> FormattedText:
        total = len(state.options)
        shown = len(state.visible)
        hint = "↑↓ navigate • Enter confirm • ESC cancel • type to filter"
        counter = f"{shown}/{total}"
        return FormattedText([("class:footer", f"{counter}  {hint}")])


class _SelectorState:
    """Mutable view-state for the running selector application."""

    def __init__(self, options: List[SelectorOption], cursor: int) -> None:
        self.options: List[SelectorOption] = options
        self.filter_text: str = ""
        self.visible: List[SelectorOption] = list(options)
        self.cursor: int = cursor
        self.app: Optional[Application] = None

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        needle = text.lower().strip()
        if not needle:
            self.visible = list(self.options)
        else:
            self.visible = [opt for opt in self.options if self._matches(opt, needle)]
        if self.cursor >= len(self.visible):
            self.cursor = max(0, len(self.visible) - 1)

    @staticmethod
    def _matches(opt: SelectorOption, needle: str) -> bool:
        haystacks = [opt.label, opt.description] + [
            str(v) for v in opt.metadata.values()
        ]
        return any(needle in h.lower() for h in haystacks if h)

    def move(self, delta: int) -> None:
        if not self.visible:
            return
        self.cursor = (self.cursor + delta) % len(self.visible)

    def set_cursor(self, idx: int) -> None:
        if not self.visible:
            return
        self.cursor = max(0, min(idx, len(self.visible) - 1))

    def current(self) -> Optional[SelectorOption]:
        if 0 <= self.cursor < len(self.visible):
            return self.visible[self.cursor]
        return None


def get_default_selector() -> PromptToolkitSelector:
    """Return a fresh default selector. The adapter is stateless."""
    return PromptToolkitSelector()
