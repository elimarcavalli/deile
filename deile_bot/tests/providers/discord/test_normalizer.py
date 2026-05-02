"""Tests for DiscordNormalizer (uses fake discord-like objects)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from deile_bot.foundation.envelope import AttachmentKind, ChannelScope
from deile_bot.providers.discord.normalizer import DiscordNormalizer


def _user(uid=111, name="alice", bot=False):
    return SimpleNamespace(id=uid, name=name, display_name=name, bot=bot)


def _channel(cid=42, name="general", cls_name="TextChannel", parent=None):
    obj = SimpleNamespace(id=cid, name=name, parent=parent)
    obj.__class__.__name__ = cls_name
    type(obj).__name__ = cls_name
    return obj


class _FakeChannel:
    def __init__(self, cid, name=None, parent=None):
        self.id = cid
        self.name = name
        self.parent = parent


class _FakeDM:
    def __init__(self, cid):
        self.id = cid
        self.name = None
        self.parent = None
    def __class__name__():
        return "DMChannel"


def _make_channel(cls_name: str, cid: int, name=None, parent=None):
    """Construct an object whose type().__name__ equals cls_name."""
    cls = type(cls_name, (), {})
    obj = cls()
    obj.id = cid
    obj.name = name
    obj.parent = parent
    return obj


def _msg(*, text="oi", channel=None, author=None, attachments=(), reference=None,
         mentions=(), msg_id=999, created_at=None):
    return SimpleNamespace(
        id=msg_id,
        content=text,
        channel=channel or _make_channel("TextChannel", 1, name="general"),
        author=author or _user(),
        attachments=list(attachments),
        reference=reference,
        mentions=list(mentions),
        created_at=created_at or datetime.now(timezone.utc),
        guild=None,
    )


class TestNormalizer:
    def test_dm_detected(self):
        ch = _make_channel("DMChannel", 5)
        env = DiscordNormalizer().to_envelope(_msg(channel=ch))
        assert env.channel.scope == ChannelScope.DM

    def test_thread_detected_with_parent(self):
        parent = _make_channel("TextChannel", 100, name="dev")
        thread = _make_channel("Thread", 200, name="bug-123", parent=parent)
        env = DiscordNormalizer().to_envelope(_msg(channel=thread))
        assert env.channel.scope == ChannelScope.THREAD
        assert env.channel.parent_channel_id == "100"

    def test_group_detected(self):
        ch = _make_channel("TextChannel", 7, name="geral")
        env = DiscordNormalizer().to_envelope(_msg(channel=ch))
        assert env.channel.scope == ChannelScope.GROUP

    def test_attachments_kind(self):
        att = SimpleNamespace(content_type="image/png", url="u", filename="x.png", size=10)
        env = DiscordNormalizer().to_envelope(_msg(attachments=[att]))
        assert env.attachments[0].kind == AttachmentKind.IMAGE

    def test_attachment_unknown_falls_back_to_file(self):
        att = SimpleNamespace(content_type="application/zip", url="u", filename="x.zip", size=10)
        env = DiscordNormalizer().to_envelope(_msg(attachments=[att]))
        assert env.attachments[0].kind == AttachmentKind.FILE

    def test_reply_resolved(self):
        replied_user = _user(uid=222, name="bob")
        resolved = SimpleNamespace(content="prior msg", author=replied_user)
        ref = SimpleNamespace(message_id=42, resolved=resolved)
        env = DiscordNormalizer().to_envelope(_msg(reference=ref))
        assert env.reply is not None
        assert env.reply.replied_message_id == "42"
        assert "prior" in env.reply.replied_excerpt

    def test_reply_broken_degrades(self):
        ref = SimpleNamespace(message_id=42, resolved=None)
        env = DiscordNormalizer().to_envelope(_msg(reference=ref))
        assert env.reply is not None
        assert env.reply.replied_excerpt == ""
        assert env.reply.replied_author.display_name == "unknown"

    def test_mentions(self):
        env = DiscordNormalizer().to_envelope(
            _msg(mentions=[_user(uid=333, name="charlie")])
        )
        assert len(env.mentions) == 1
        assert env.mentions[0].provider_user_id == "333"

    def test_naive_datetime_normalized_to_utc(self):
        env = DiscordNormalizer().to_envelope(_msg(created_at=datetime(2026, 5, 2)))
        assert env.sent_at.tzinfo is not None
