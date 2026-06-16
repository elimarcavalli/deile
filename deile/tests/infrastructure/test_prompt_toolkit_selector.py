"""Unit tests for the prompt_toolkit selector adapter.

The renderer and key bindings are exercised through the internal
:class:`_SelectorState` (filter, cursor wrap, default-index clamp) plus
narrow asserts on :meth:`PromptToolkitSelector.is_supported`. Driving a real
``prompt_toolkit.Application`` would require a PTY harness — out of scope.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from deile.core.interfaces.selector import (
    InteractiveSelector,
    SelectorNotSupported,
    SelectorOption,
)
from deile.infrastructure.selectors.prompt_toolkit_selector import (
    PromptToolkitSelector,
    _SelectorState,
    get_default_selector,
)


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

        with (
            patch.object(PromptToolkitSelector, "is_supported", return_value=True),
            patch(
                "deile.infrastructure.selectors.prompt_toolkit_selector.Application.run_async",
                _fake_run_async,
            ),
        ):
            result = await sel.select([chosen, SelectorOption(label="b", value=99)])
            assert result is chosen

    @pytest.mark.asyncio
    async def test_select_returns_none_on_cancel(self):
        sel = PromptToolkitSelector()

        async def _fake_run_async(self, *args, **kwargs):
            return None

        with (
            patch.object(PromptToolkitSelector, "is_supported", return_value=True),
            patch(
                "deile.infrastructure.selectors.prompt_toolkit_selector.Application.run_async",
                _fake_run_async,
            ),
        ):
            result = await sel.select([SelectorOption(label="a", value=1)])
            assert result is None


class TestGetDefaultSelector:
    def test_returns_a_prompt_toolkit_selector(self):
        sel = get_default_selector()
        assert isinstance(sel, InteractiveSelector)
        assert isinstance(sel, PromptToolkitSelector)


class TestRender:
    def test_render_includes_prompt_and_rows(self):
        opts = _make_options(3)
        state = _SelectorState(options=opts, cursor=1)
        rendered = PromptToolkitSelector._render("Pick one", state)
        flat = "".join(text for _, text in rendered)
        assert "Pick one" in flat
        assert "item-0" in flat and "item-1" in flat and "item-2" in flat
        # The cursor row carries the selection marker.
        assert "▶ item-1" in flat

    def test_render_empty_state_when_filter_matches_nothing(self):
        state = _SelectorState(options=_make_options(3), cursor=0)
        state.set_filter("zzz-no-match")
        rendered = PromptToolkitSelector._render("Pick one", state)
        flat = "".join(text for _, text in rendered)
        assert "(no matches)" in flat
        assert "filter: zzz-no-match" in flat

    def test_render_includes_description_when_present(self):
        opts = [SelectorOption(label="row", value=1, description="hello world")]
        state = _SelectorState(options=opts, cursor=0)
        rendered = PromptToolkitSelector._render("p", state)
        flat = "".join(text for _, text in rendered)
        assert "hello world" in flat

    def test_render_footer_shows_counts_and_hint(self):
        state = _SelectorState(options=_make_options(5), cursor=0)
        state.set_filter("item-1")  # filters down to 1
        footer = PromptToolkitSelector._render_footer(state)
        flat = "".join(text for _, text in footer)
        assert "1/5" in flat
        assert "ESC cancel" in flat


class TestSelectMapsTerminalErrorToNotSupported:
    @pytest.mark.asyncio
    async def test_runtime_error_from_app_is_mapped(self):
        sel = PromptToolkitSelector()

        async def _boom(self, *args, **kwargs):
            raise RuntimeError("no console screen buffer")

        with (
            patch.object(PromptToolkitSelector, "is_supported", return_value=True),
            patch(
                "deile.infrastructure.selectors.prompt_toolkit_selector.Application.run_async",
                _boom,
            ),
        ):
            with pytest.raises(SelectorNotSupported):
                await sel.select([SelectorOption(label="x", value=1)])

    @pytest.mark.asyncio
    async def test_os_error_from_app_is_mapped(self):
        sel = PromptToolkitSelector()

        async def _boom(self, *args, **kwargs):
            raise OSError("redirected stdin")

        with (
            patch.object(PromptToolkitSelector, "is_supported", return_value=True),
            patch(
                "deile.infrastructure.selectors.prompt_toolkit_selector.Application.run_async",
                _boom,
            ),
        ):
            with pytest.raises(SelectorNotSupported):
                await sel.select([SelectorOption(label="x", value=1)])
