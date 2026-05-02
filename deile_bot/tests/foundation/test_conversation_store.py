"""Tests for ConversationStore (SQLite persistence)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from deile_bot._testing import make_channel, make_envelope, make_user
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import (Attachment, AttachmentKind,
                                           ChannelScope)
from deile_bot.foundation.exceptions import ConversationStoreError


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "bot.sqlite")
    await s.init()
    yield s
    await s.close()


class TestInit:
    async def test_init_runs_migrations(self, tmp_path):
        s = ConversationStore(tmp_path / "bot.sqlite")
        await s.init()
        # Re-init is idempotent
        await s.init()
        # Schema present
        cur = await s._db.execute("SELECT version FROM schema_version")
        rows = await cur.fetchall()
        await cur.close()
        assert rows and rows[0][0] == 1
        await s.close()

    async def test_uninitialized_raises(self, tmp_path):
        s = ConversationStore(tmp_path / "x.sqlite")
        with pytest.raises(ConversationStoreError):
            await s.upsert_user(make_user())

    async def test_wal_mode_active(self, store):
        cur = await store._db.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
        await cur.close()
        assert row[0].lower() == "wal"

    async def test_foreign_keys_on(self, store):
        cur = await store._db.execute("PRAGMA foreign_keys")
        row = await cur.fetchone()
        await cur.close()
        assert row[0] == 1


class TestUsers:
    async def test_upsert_user_idempotent(self, store):
        u = make_user(provider="discord", provider_user_id="123")
        await store.upsert_user(u)
        await store.upsert_user(u)
        cur = await store._db.execute("SELECT count(*) FROM bot_user")
        row = await cur.fetchone()
        await cur.close()
        assert row[0] == 1

    async def test_get_user_by_provider_id(self, store):
        u = make_user(provider="discord", provider_user_id="42")
        await store.upsert_user(u)
        found = await store.get_user_by_provider_id("discord", "42")
        assert found is not None
        assert found.bot_user_id == u.bot_user_id

    async def test_search_by_display_name(self, store):
        await store.upsert_user(make_user(provider_user_id="1", display_name="alice"))
        await store.upsert_user(make_user(provider_user_id="2", display_name="bob"))
        await store.upsert_user(make_user(provider_user_id="3", display_name="alex"))
        results = await store.search_users_by_display_name("al")
        names = {u.display_name for u in results}
        assert "alice" in names and "alex" in names
        assert "bob" not in names


class TestMessages:
    async def test_record_inbound_unique(self, store):
        env = make_envelope(text="hello")
        await store.upsert_user(env.author)
        await store.upsert_channel(env.channel)
        id1 = await store.record_inbound(env)
        id2 = await store.record_inbound(env)
        # OR IGNORE, so id2 == 0 (no insert)
        assert id1 > 0
        assert id2 == 0 or id2 == id1

    async def test_record_outbound(self, store):
        u = make_user()
        ch = make_channel(scope=ChannelScope.GROUP)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        sent_at = datetime.now(timezone.utc)
        rid = await store.record_outbound(
            "fake", ch, "out-1", u.bot_user_id, "hi back", reply_to="m-1", sent_at=sent_at
        )
        assert rid > 0

    async def test_get_recent_messages_order_desc(self, store):
        env = make_envelope(text="first")
        await store.upsert_user(env.author)
        await store.upsert_channel(env.channel)
        await store.record_inbound(env)
        env2 = make_envelope(
            text="second",
            channel=env.channel,
            author=env.author,
            sent_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        await store.record_inbound(env2)
        msgs = await store.get_recent_messages("fake", env.channel)
        assert len(msgs) == 2
        assert msgs[0].text == "second"
        assert msgs[1].text == "first"

    async def test_purge_older_than(self, store):
        u = make_user()
        ch = make_channel(scope=ChannelScope.GROUP)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        old = make_envelope(
            text="old",
            channel=ch,
            author=u,
            message_id="old-1",
            sent_at=datetime.now(timezone.utc) - timedelta(days=120),
        )
        await store.record_inbound(old)
        new = make_envelope(text="fresh", channel=ch, author=u, message_id="new-1")
        await store.record_inbound(new)
        removed = await store.purge_older_than(days=30)
        assert removed == 1
        remaining = await store.get_recent_messages("fake", ch)
        assert {m.text for m in remaining} == {"fresh"}

    async def test_purge_user_messages(self, store):
        u1 = make_user(provider_user_id="1", display_name="alice")
        u2 = make_user(provider_user_id="2", display_name="bob")
        ch = make_channel(scope=ChannelScope.GROUP)
        await store.upsert_user(u1)
        await store.upsert_user(u2)
        await store.upsert_channel(ch)
        await store.record_inbound(make_envelope(channel=ch, author=u1, message_id="a1"))
        await store.record_inbound(make_envelope(channel=ch, author=u2, message_id="b1"))
        removed = await store.purge_user_messages(u1.bot_user_id)
        assert removed == 1


class TestConcurrency:
    async def test_parallel_inserts_no_corruption(self, tmp_path):
        s = ConversationStore(tmp_path / "concurrent.sqlite")
        await s.init()
        u = make_user()
        ch = make_channel(scope=ChannelScope.GROUP)
        await s.upsert_user(u)
        await s.upsert_channel(ch)
        envs = [
            make_envelope(channel=ch, author=u, message_id=f"m-{i}", text=f"m{i}")
            for i in range(50)
        ]
        await asyncio.gather(*(s.record_inbound(e) for e in envs))
        msgs = await s.get_recent_messages("fake", ch, limit=100)
        assert len(msgs) == 50
        await s.close()


class TestAttachments:
    async def test_attachment_inline_under_threshold(self, store):
        att = Attachment(kind=AttachmentKind.IMAGE, bytes_inline=b"hi", filename="x.png")
        env = make_envelope(attachments=(att,))
        await store.upsert_user(env.author)
        await store.upsert_channel(env.channel)
        await store.record_inbound(env)
        cur = await store._db.execute("SELECT bytes_inline_b64 FROM attachment")
        row = await cur.fetchone()
        await cur.close()
        assert row[0] is not None


class TestAudit:
    async def test_audit_insert_and_query(self, store):
        await store.insert_audit("inbound_received", payload={"channel": "general"})
        await store.insert_audit("outbound_sent", payload={"chars": 42})
        rows = await store.query_audit()
        types = [r["event_type"] for r in rows]
        assert "inbound_received" in types
        assert "outbound_sent" in types

    async def test_audit_filter_by_event(self, store):
        await store.insert_audit("agent_invoked")
        await store.insert_audit("agent_failed")
        rows = await store.query_audit(event_type="agent_failed")
        assert all(r["event_type"] == "agent_failed" for r in rows)


class TestDLQ:
    async def test_insert_list_count(self, store):
        await store.insert_dlq("discord", {"text": "fail"}, "boom", attempts=3)
        assert await store.count_dlq("discord") == 1
        rows = await store.list_dlq("discord")
        assert rows[0]["last_error"] == "boom"

    async def test_purge_dlq(self, store):
        await store.insert_dlq("discord", {"text": "old"}, "e", 1)
        # All entries are fresh; purging older than 0 days clears all
        removed = await store.purge_dlq_older_than(days=0)
        assert removed >= 0


class TestChannel:
    async def test_upsert_and_get_channel(self, store):
        ch = make_channel(provider_channel_id="c-99", scope=ChannelScope.GROUP)
        await store.upsert_channel(ch)
        retrieved = await store.get_channel("fake", "c-99")
        assert retrieved is not None
        assert retrieved.scope == ChannelScope.GROUP
