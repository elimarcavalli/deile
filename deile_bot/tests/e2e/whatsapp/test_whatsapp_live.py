"""WhatsApp live E2E (ready-skipped)."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.e2e_whatsapp_live,
    pytest.mark.skipif(
        not os.environ.get("WHATSAPP_TOKEN") and not os.environ.get("DEILE_BOT_WHATSAPP_ACCESS_TOKEN"),
        reason="requires live WHATSAPP_TOKEN",
    ),
]


async def test_we2e_send_text():
    pytest.skip("manual — requires WhatsApp Business test number")


async def test_we2e_template_outside_window():
    pytest.skip("manual — requires approved template")


async def test_we2e_conversation_window_24h():
    pytest.skip("manual — requires waiting + outbound retry")
