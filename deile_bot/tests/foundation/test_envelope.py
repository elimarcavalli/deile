"""Tests for envelope DTOs (inbound + outbound + window)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from deile_bot.foundation.envelope import (Attachment, AttachmentKind, BotUser,
                                           Channel, ChannelScope,
                                           ConversationWindow, MessageEnvelope,
                                           OutboundEnvelope, OutboundIntent,
                                           ReplyContext, TemplateMessage)


def _user(**kw):
    defaults = dict(
        bot_user_id="01HZ-alice",
        provider="discord",
        provider_user_id="111",
        display_name="alice",
    )
    defaults.update(kw)
    return BotUser(**defaults)


def _channel(**kw):
    defaults = dict(
        provider="discord",
        provider_channel_id="ch1",
        name="general",
        scope=ChannelScope.GROUP,
    )
    defaults.update(kw)
    return Channel(**defaults)


def _envelope(**kw):
    defaults = dict(
        message_id="m1",
        channel=_channel(),
        author=_user(),
        sent_at=datetime.now(timezone.utc),
        text="hello",
    )
    defaults.update(kw)
    return MessageEnvelope(**defaults)


class TestBotUser:
    def test_frozen_blocks_mutation(self):
        u = _user()
        with pytest.raises(Exception):
            u.display_name = "x"  # type: ignore[misc]

    def test_empty_id_raises(self):
        with pytest.raises(ValueError):
            BotUser(bot_user_id="", provider="x", provider_user_id="y", display_name="z")

    def test_empty_provider_raises(self):
        with pytest.raises(ValueError):
            BotUser(bot_user_id="a", provider="", provider_user_id="y", display_name="z")


class TestChannel:
    def test_thread_requires_parent(self):
        with pytest.raises(ValueError):
            Channel(provider="discord", provider_channel_id="t1", scope=ChannelScope.THREAD)

    def test_thread_with_parent_ok(self):
        c = Channel(
            provider="discord",
            provider_channel_id="t1",
            scope=ChannelScope.THREAD,
            parent_channel_id="ch1",
        )
        assert c.scope == ChannelScope.THREAD


class TestMessageEnvelope:
    def test_minimum_envelope(self):
        e = _envelope()
        assert e.message_id == "m1"
        assert not e.has_attachments
        assert e.is_dm is False

    def test_naive_sent_at_raises(self):
        with pytest.raises(ValueError):
            _envelope(sent_at=datetime.now())

    def test_empty_message_id_raises(self):
        with pytest.raises(ValueError):
            _envelope(message_id="")

    def test_dm_detection(self):
        e = _envelope(channel=_channel(scope=ChannelScope.DM))
        assert e.is_dm

    def test_mentions_self(self):
        bot = _user(provider_user_id="bot-9", display_name="DEILE")
        e = _envelope(mentions=(bot,))
        assert e.mentions_self("bot-9")
        assert not e.mentions_self("nope")
        assert not e.mentions_self("")

    def test_force_respond_contract(self):
        e1 = _envelope(raw=MappingProxyType({"force_respond": True}))
        e2 = _envelope()
        assert e1.has_force_respond
        assert not e2.has_force_respond

    def test_attachments_must_be_tuple(self):
        with pytest.raises(TypeError):
            _envelope(attachments=[Attachment(kind=AttachmentKind.IMAGE)])

    def test_has_attachments(self):
        e = _envelope(attachments=(Attachment(kind=AttachmentKind.IMAGE, url="u"),))
        assert e.has_attachments

    def test_immutable(self):
        e = _envelope()
        with pytest.raises(Exception):
            e.text = "other"  # type: ignore[misc]


class TestAttachment:
    def test_inline_property(self):
        a1 = Attachment(kind=AttachmentKind.FILE, bytes_inline=b"abc")
        a2 = Attachment(kind=AttachmentKind.FILE, url="http://x")
        assert a1.is_inline
        assert not a2.is_inline


class TestReplyContext:
    def test_empty_replied_id_raises(self):
        with pytest.raises(ValueError):
            ReplyContext(replied_message_id="", replied_author=_user(), replied_excerpt="x")


class TestOutbound:
    def test_free_text_requires_text(self):
        with pytest.raises(ValueError):
            OutboundEnvelope(intent=OutboundIntent.FREE_TEXT)

    def test_free_text_ok(self):
        out = OutboundEnvelope(intent=OutboundIntent.FREE_TEXT, text="hi")
        assert not out.requires_template_window

    def test_template_requires_template(self):
        with pytest.raises(ValueError):
            OutboundEnvelope(intent=OutboundIntent.TEMPLATE)

    def test_template_ok(self):
        tpl = TemplateMessage(name="welcome", language="pt_BR")
        out = OutboundEnvelope(intent=OutboundIntent.TEMPLATE, template=tpl)
        assert out.requires_template_window


class TestConversationWindow:
    def test_open_when_recent(self):
        win = ConversationWindow(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=1),
            window_hours=24,
        )
        assert win.is_open

    def test_closed_when_stale(self):
        win = ConversationWindow(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=48),
            window_hours=24,
        )
        assert not win.is_open

    def test_closed_when_never(self):
        win = ConversationWindow(last_inbound_at=None, window_hours=24)
        assert not win.is_open

    def test_window_hours_must_be_positive(self):
        with pytest.raises(ValueError):
            ConversationWindow(last_inbound_at=None, window_hours=0)
