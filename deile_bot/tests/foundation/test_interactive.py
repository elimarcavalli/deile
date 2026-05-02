"""Tests for interactive controls."""

from __future__ import annotations

import pytest

from deile_bot.foundation.interactive import (InteractiveButton,
                                              InteractiveButtonRow,
                                              InteractiveList,
                                              InteractiveListSection,
                                              QuickReplies, QuickReply)


class TestInteractiveButton:
    def test_url_only(self):
        b = InteractiveButton(label="open", url="http://x")
        assert b.url == "http://x"

    def test_callback_only(self):
        b = InteractiveButton(label="x", callback_data="cb")
        assert b.callback_data == "cb"

    def test_either_required(self):
        with pytest.raises(ValueError):
            InteractiveButton(label="x")

    def test_label_required(self):
        with pytest.raises(ValueError):
            InteractiveButton(label="", callback_data="cb")


class TestButtonRow:
    def test_max_5(self):
        buttons = tuple(InteractiveButton(label=str(i), callback_data=f"c{i}") for i in range(6))
        with pytest.raises(ValueError):
            InteractiveButtonRow(buttons=buttons)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            InteractiveButtonRow(buttons=())

    def test_ok(self):
        row = InteractiveButtonRow(
            buttons=(InteractiveButton(label="a", callback_data="x"),)
        )
        assert len(row.buttons) == 1


class TestList:
    def test_section_max_10(self):
        items = tuple(InteractiveButton(label=str(i), callback_data=f"c{i}") for i in range(11))
        with pytest.raises(ValueError):
            InteractiveListSection(title="s", items=items)

    def test_list_needs_section(self):
        with pytest.raises(ValueError):
            InteractiveList(button_label="open", sections=())


class TestQuickReplies:
    def test_max_13(self):
        opts = tuple(QuickReply(label=str(i), payload=str(i)) for i in range(14))
        with pytest.raises(ValueError):
            QuickReplies(options=opts)

    def test_empty(self):
        with pytest.raises(ValueError):
            QuickReplies(options=())
