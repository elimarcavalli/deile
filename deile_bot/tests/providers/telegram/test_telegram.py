"""Smoke tests for TelegramAdapter/Formatter/Normalizer."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.foundation.envelope import ChannelScope
from deile_bot.foundation.exceptions import ProviderError
from deile_bot.providers.telegram.adapter import TelegramAdapter
from deile_bot.providers.telegram.formatter import (TelegramOutputFormatter,
                                                    escape_markdown_v2)
from deile_bot.providers.telegram.normalizer import TelegramNormalizer
from deile_bot.providers.telegram.settings import (TELEGRAM_CAPABILITIES,
                                                   TelegramBotSettings)


class TestCapabilities:
    def test_telegram_caps(self):
        assert TELEGRAM_CAPABILITIES.can_inline_keyboards is True
        assert TELEGRAM_CAPABILITIES.max_message_chars == 4096


class TestFormatter:
    def test_escape(self):
        assert escape_markdown_v2("hello.world") == r"hello\.world"

    def test_render_bold(self):
        f = TelegramOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.BOLD, "x")])
        assert f.render(ast) == "*x*"

    def test_render_italic(self):
        f = TelegramOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.ITALIC, "y")])
        assert f.render(ast) == "_y_"

    def test_render_link(self):
        f = TelegramOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.LINK, "doc", meta={"url": "http://x"})])
        assert f.render(ast) == "[doc](http://x)"


class TestNormalizer:
    def test_normalize_dm(self):
        msg = SimpleNamespace(
            message_id=42,
            chat=SimpleNamespace(id=111, type="private", title=None, username="alice"),
            from_user=SimpleNamespace(id=111, full_name="Alice", is_bot=False, username="alice"),
            text="oi DEILE",
            caption=None,
            date=datetime.now(timezone.utc),
            photo=None,
            document=None,
            reply_to_message=None,
        )
        env = TelegramNormalizer().to_envelope(msg)
        assert env.channel.scope == ChannelScope.DM
        assert env.author.provider_user_id == "111"
        assert env.text == "oi DEILE"


class TestAdapter:
    def test_caps(self):
        a = TelegramAdapter(TelegramBotSettings(token=SecretStr("t")))
        assert a.name == "telegram"
        assert a.capabilities == TELEGRAM_CAPABILITIES

    async def test_start_no_token_raises(self):
        a = TelegramAdapter(TelegramBotSettings(token=SecretStr("")))
        with pytest.raises(ProviderError):
            await a.start()
