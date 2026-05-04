"""Domain-contract tests for :mod:`deile.core.interfaces.selector`."""

from __future__ import annotations

from typing import Optional, Sequence

import pytest

from deile.core.exceptions import DEILEError
from deile.core.interfaces.selector import (InteractiveSelector,
                                            SelectorNotSupported,
                                            SelectorOption)


class TestSelectorOption:
    def test_label_and_value_required_positional(self):
        opt = SelectorOption(label="Anthropic Claude", value="anthropic:claude-opus-4-7")
        assert opt.label == "Anthropic Claude"
        assert opt.value == "anthropic:claude-opus-4-7"
        assert opt.description == ""
        assert opt.metadata == {}

    def test_dataclass_is_frozen(self):
        opt = SelectorOption(label="x", value=1)
        with pytest.raises(Exception):
            opt.label = "y"  # type: ignore[misc]

    def test_metadata_default_is_independent_per_instance(self):
        a = SelectorOption(label="a", value=1)
        b = SelectorOption(label="b", value=2)
        assert a.metadata is not b.metadata


class TestSelectorExceptions:
    def test_not_supported_inherits_from_deile_error(self):
        assert issubclass(SelectorNotSupported, DEILEError)


class _FakeSelector(InteractiveSelector):
    """Minimal in-memory selector used to exercise the abstract contract."""

    def __init__(self, supported: bool = True, choice_index: Optional[int] = 0):
        self._supported = supported
        self._choice_index = choice_index
        self.last_call: Optional[dict] = None

    def is_supported(self) -> bool:
        return self._supported

    async def select(
        self,
        options: Sequence[SelectorOption],
        *,
        prompt: str = "Select an option",
        default_index: int = 0,
    ) -> Optional[SelectorOption]:
        self.last_call = {
            "options": list(options),
            "prompt": prompt,
            "default_index": default_index,
        }
        if not options:
            raise ValueError("options must not be empty")
        if not self.is_supported():
            raise SelectorNotSupported("no TTY")
        if self._choice_index is None:
            return None
        return options[self._choice_index]


class TestInteractiveSelectorContract:
    @pytest.mark.asyncio
    async def test_returns_chosen_option(self):
        sel = _FakeSelector(choice_index=1)
        opts = [SelectorOption(label=f"o{i}", value=i) for i in range(3)]
        result = await sel.select(opts, prompt="Pick one")
        assert result is not None
        assert result.value == 1
        assert sel.last_call["prompt"] == "Pick one"

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel(self):
        sel = _FakeSelector(choice_index=None)
        opts = [SelectorOption(label="o", value=1)]
        assert await sel.select(opts) is None

    @pytest.mark.asyncio
    async def test_empty_options_raises_value_error(self):
        sel = _FakeSelector()
        with pytest.raises(ValueError):
            await sel.select([])

    @pytest.mark.asyncio
    async def test_unsupported_raises_selector_not_supported(self):
        sel = _FakeSelector(supported=False)
        opts = [SelectorOption(label="o", value=1)]
        with pytest.raises(SelectorNotSupported):
            await sel.select(opts)

    @pytest.mark.asyncio
    async def test_default_index_is_passed_through(self):
        sel = _FakeSelector(choice_index=0)
        opts = [SelectorOption(label="o", value=i) for i in range(3)]
        await sel.select(opts, default_index=2)
        assert sel.last_call["default_index"] == 2

    def test_cannot_instantiate_abstract_selector(self):
        with pytest.raises(TypeError):
            InteractiveSelector()  # type: ignore[abstract]
