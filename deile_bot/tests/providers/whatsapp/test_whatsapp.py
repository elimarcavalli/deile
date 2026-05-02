"""Smoke tests for WhatsApp adapter + normalizer + formatter."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.foundation.envelope import ChannelScope, ConversationWindow
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deile_bot.providers.whatsapp.adapter import WhatsAppAdapter
from deile_bot.providers.whatsapp.formatter import WhatsAppFormatter
from deile_bot.providers.whatsapp.normalizer import WhatsAppNormalizer
from deile_bot.providers.whatsapp.settings import (WHATSAPP_CAPABILITIES,
                                                   WhatsAppSettings)


class TestCapabilities:
    def test_window_active(self):
        assert WHATSAPP_CAPABILITIES.has_conversation_window is True
        assert WHATSAPP_CAPABILITIES.can_edit_message is False


class TestFormatter:
    def test_bold_italic(self):
        f = WhatsAppFormatter()
        ast = MarkupAST([
            MarkupSpan(SpanKind.BOLD, "B"),
            MarkupSpan(SpanKind.PLAIN, " "),
            MarkupSpan(SpanKind.ITALIC, "I"),
        ])
        assert f.render(ast) == "*B* _I_"


class TestNormalizer:
    def test_text_message(self):
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "100"},
                        "contacts": [{"wa_id": "5511999", "profile": {"name": "Alice"}}],
                        "messages": [{
                            "id": "wamid.XYZ",
                            "from": "5511999",
                            "type": "text",
                            "text": {"body": "ola"},
                            "timestamp": "1730000000",
                        }],
                    },
                }],
            }],
        }
        env = WhatsAppNormalizer().to_envelope(payload)
        assert env is not None
        assert env.text == "ola"
        assert env.channel.scope == ChannelScope.DM
        assert env.author.display_name == "Alice"


class TestAdapter:
    def test_caps(self):
        a = WhatsAppAdapter(WhatsAppSettings(access_token=SecretStr("t"), phone_number_id="100"))
        assert a.name == "whatsapp"
        assert not a.capabilities.can_edit_message

    async def test_start_no_token_raises(self):
        a = WhatsAppAdapter(WhatsAppSettings())
        with pytest.raises(ProviderError):
            await a.start()

    async def test_edit_unsupported(self):
        a = WhatsAppAdapter(WhatsAppSettings(access_token=SecretStr("t"), phone_number_id="100"))
        from deile_bot._testing import make_channel

        with pytest.raises(CapabilityNotSupported):
            await a.edit_message(make_channel(), "x", "new")


class TestConversationWindow:
    def test_window_dto(self):
        from datetime import datetime, timedelta, timezone

        win = ConversationWindow(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=1),
            window_hours=24,
        )
        assert win.is_open
