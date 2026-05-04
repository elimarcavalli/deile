"""Unit tests for the prompt_toolkit selector adapter.

The renderer and key bindings are exercised through the internal
:class:`_SelectorState` (filter, cursor wrap, default-index clamp) plus
narrow asserts on :meth:`PromptToolkitSelector.is_supported`. Driving a real
``prompt_toolkit.Application`` would require a PTY harness — out of scope.
"""

from __future__ import annotations

from typing import Optional, Sequence
from unittest.mock import patch

import pytest

from deile.core.interfaces.selector import (InteractiveSelector,
                                            SelectorNotSupported,
                                            SelectorOption)
from deile.infrastructure.selectors.prompt_toolkit_selector import (
    PromptToolkitSelector, _SelectorState, get_default_selector)


def _make_options(n: int) -> list[SelectorOption]:
    return [
        SelectorOption(label=f"item-{i}", value=i, description=f"d{i}")
        for i in range(n)
    ]


class TestSelectorState:
    def test_no_filter_keeps_all_visible(self):
        opts = _make_options(5)
        s = _SelectorState(options=opts, cursor=0)
        assert s.visible == opts

    def test_filter_substring_case_insensitive(self):
        opts = [
            SelectorOption(label="Anthropic Claude", value=1),
            SelectorOption(label="OpenAI GPT-4o", value=2),
            SelectorOption(label="Google Gemini", value=3),
        ]
        s = _SelectorState(options=opts, cursor=0)
        s.set_filter("claude")
        assert [o.value for o in s.visible] == [1]
        s.set_filter("AI")  # matches "OpenAI"
        assert [o.value for o in s.visible] == [2]
        s.set_filter("")
        assert s.visible == opts

    def test_filter_matches_description_and_metadata(self):
        opts = [
            SelectorOption(label="row1", value=1, description="cheap"),
            SelectorOption(label="row2", value=2, metadata={"tier": "fast"}),
        ]
        s = _SelectorState(options=opts, cursor=0)
        s.set_filter("cheap")
        assert [o.value for o in s.visible] == [1]
        s.set_filter("fast")
        assert [o.value for o in s.visible] == [2]

    def test_cursor_wraps_on_move(self):
        s = _SelectorState(options=_make_options(3), cursor=0)
        s.move(-1)
        assert s.cursor == 2
        s.move(1)
        assert s.cursor == 0
        s.move(2)
        assert s.cursor == 2

    def test_filter_clamps_cursor(self):
        s = _SelectorState(options=_make_options(5), cursor=4)
        s.set_filter("item-0")  # only first row matches
        assert s.cursor == 0
        assert len(s.visible) == 1

    def test_current_returns_visible_row(self):
        s = _SelectorState(options=_make_options(3), cursor=1)
        opt = s.current()
        assert opt is not None
        assert opt.value == 1

    def test_current_returns_none_when_empty(self):
        s = _SelectorState(options=_make_options(3), cursor=0)
        s.set_filter("nonexistent")
        assert s.current() is None

    def test_set_cursor_clamps_to_range(self):
        s = _SelectorState(options=_make_options(3), cursor=0)
        s.set_cursor(99)
        assert s.cursor == 2
        s.set_cursor(-5)
        assert s.cursor == 0


class TestPromptToolkitSelectorIsSupported:
    def test_returns_true_when_both_streams_are_tty(self):
        sel = PromptToolkitSelector()
        with patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            assert sel.is_supported() is True

    def test_returns_false_when_stdin_not_tty(self):
        sel = PromptToolkitSelector()
        with patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
            stdin.isatty.return_value = False
            stdout.isatty.return_value = True
            assert sel.is_supported() is False

    def test_returns_false_when_stdout_not_tty(self):
        sel = PromptToolkitSelector()
        with patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
            stdin.isatty.return_value = True
            stdout.isatty.return_value = False
            assert sel.is_supported() is False

    def test_returns_false_when_isatty_raises(self):
        sel = PromptToolkitSelector()
        with patch("sys.stdin") as stdin:
            stdin.isatty.side_effect = ValueError("closed stream")
            assert sel.is_supported() is False


class TestPromptToolkitSelectorSelect:
    @pytest.mark.asyncio
    async def test_empty_options_raises_value_error(self):
        sel = PromptToolkitSelector()
        with pytest.raises(ValueError):
            await sel.select([])

    @pytest.mark.asyncio
    async def test_unsupported_environment_raises(self):
        sel = PromptToolkitSelector()
        with patch.object(PromptToolkitSelector, "is_supported", return_value=False):
            with pytest.raises(SelectorNotSupported):
                await sel.select([SelectorOption(label="x", value=1)])

    @pytest.mark.asyncio
    async def test_select_returns_app_result_when_option(self):
        sel = PromptToolkitSelector()
        chosen = SelectorOption(label="a", value=42)

        async def _fake_run_async(self, *args, **kwargs):
            return chosen

        with patch.object(PromptToolkitSelector, "is_supported", return_value=True), \
             patch(
                 "deile.infrastructure.selectors.prompt_toolkit_selector.Application.run_async",
                 _fake_run_async,
             ):
            result = await sel.select([chosen, SelectorOption(label="b", value=99)])
            assert result is chosen

    @pytest.mark.asyncio
    async def test_select_returns_none_on_cancel(self):
        sel = PromptToolkitSelector()

        async def _fake_run_async(self, *args, **kwargs):
            return None

        with patch.object(PromptToolkitSelector, "is_supported", return_value=True), \
             patch(
                 "deile.infrastructure.selectors.prompt_toolkit_selector.Application.run_async",
                 _fake_run_async,
             ):
            result = await sel.select([SelectorOption(label="a", value=1)])
            assert result is None


class TestGetDefaultSelector:
    def test_returns_singleton_instance(self):
        a = get_default_selector()
        b = get_default_selector()
        assert a is b
        assert isinstance(a, InteractiveSelector)


class _RecordingSelector(InteractiveSelector):
    """Selector that returns a preconfigured option without any I/O."""

    def __init__(self, supported: bool, choice: Optional[SelectorOption]):
        self._supported = supported
        self._choice = choice
        self.calls: list[dict] = []

    def is_supported(self) -> bool:
        return self._supported

    async def select(
        self,
        options: Sequence[SelectorOption],
        *,
        prompt: str = "Select an option",
        default_index: int = 0,
    ) -> Optional[SelectorOption]:
        self.calls.append(
            {"options": list(options), "prompt": prompt, "default_index": default_index}
        )
        return self._choice
