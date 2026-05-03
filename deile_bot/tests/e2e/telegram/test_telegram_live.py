"""Telegram live E2E (ready-skipped)."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.e2e_telegram_live,
    pytest.mark.skipif(
        not os.environ.get("TELEGRAM_BOT_TOKEN") and not os.environ.get("DEILE_BOT_TELEGRAM_TOKEN"),
        reason="requires live TELEGRAM_BOT_TOKEN",
    ),
]


async def test_te2e_polling_connects():
    pytest.skip("manual — requires bot token + test chat")


async def test_te2e_botcommands_sync():
    pytest.skip("manual — verify auto-complete in Telegram client")


async def test_te2e_inline_keyboard():
    pytest.skip("manual")


async def test_te2e_streaming_via_edit():
    pytest.skip("manual")
