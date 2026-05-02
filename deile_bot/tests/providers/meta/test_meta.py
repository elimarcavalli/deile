"""Smoke tests for Messenger + Instagram adapters."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deile_bot.providers.meta._common.settings import MetaCommonSettings
from deile_bot.providers.meta.instagram.adapter import (INSTAGRAM_CAPABILITIES,
                                                        InstagramAdapter)
from deile_bot.providers.meta.messenger.adapter import (MESSENGER_CAPABILITIES,
                                                        MessengerAdapter)


class TestMessenger:
    def test_caps(self):
        assert MESSENGER_CAPABILITIES.has_conversation_window is True
        assert MESSENGER_CAPABILITIES.can_edit_message is False

    async def test_start_no_token(self):
        a = MessengerAdapter(MetaCommonSettings())
        with pytest.raises(ProviderError):
            await a.start()

    async def test_normalize_text_msg(self):
        a = MessengerAdapter(MetaCommonSettings(page_access_token=SecretStr("t"), page_id="999"))
        payload = {
            "entry": [{
                "messaging": [{
                    "sender": {"id": "user-1"},
                    "recipient": {"id": "999"},
                    "timestamp": 1730000000000,
                    "message": {"mid": "mid.X", "text": "oi"},
                }],
            }],
        }
        seen = []

        async def cb(env, src):
            seen.append(env)

        a.on_inbound = cb
        await a.handle_webhook(payload)
        assert len(seen) == 1
        assert seen[0].text == "oi"

    async def test_edit_unsupported(self):
        a = MessengerAdapter(MetaCommonSettings(page_access_token=SecretStr("t"), page_id="999"))
        from deile_bot._testing import make_channel

        with pytest.raises(CapabilityNotSupported):
            await a.edit_message(make_channel(), "x", "y")


class TestInstagram:
    def test_inherits_from_messenger(self):
        assert issubclass(InstagramAdapter, MessengerAdapter)

    def test_distinct_caps(self):
        assert INSTAGRAM_CAPABILITIES.can_react is False
        assert INSTAGRAM_CAPABILITIES.max_message_chars == 1000
