"""Tests for the memory.json -> SQLite migration script."""

from __future__ import annotations

import json

import pytest

from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import Channel, ChannelScope
from scripts.migrate_memory_json_to_sqlite import migrate

SAMPLE = {
    "channels": {
        "1234": {
            "channel_name": "geral",
            "messages": [
                {
                    "message_id": "m1",
                    "user_id": "111",
                    "display_name": "alice",
                    "content": "oi",
                    "timestamp": "2026-04-01T10:00:00Z",
                    "bot_response": "ola alice",
                },
                {
                    "message_id": "m2",
                    "user_id": "222",
                    "display_name": "bob",
                    "content": "tudo bem?",
                    "timestamp": "2026-04-01T10:05:00Z",
                },
            ],
        }
    }
}


@pytest.fixture
async def memory_file(tmp_path):
    p = tmp_path / "memory.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    return p


class TestMigration:
    async def test_dry_run_does_not_write(self, memory_file, tmp_path):
        target = tmp_path / "bot.sqlite"
        rc = await migrate(memory_file, target, dry_run=True)
        assert rc == 0
        assert not target.exists()

    async def test_full_migration(self, memory_file, tmp_path):
        target = tmp_path / "bot.sqlite"
        rc = await migrate(memory_file, target, dry_run=False)
        assert rc == 0
        # Verify rows
        store = ConversationStore(target)
        await store.init()
        ch = Channel(provider="discord", provider_channel_id="1234", name="geral", scope=ChannelScope.GROUP)
        msgs = await store.get_recent_messages("discord", ch, limit=100)
        assert len(msgs) >= 3  # 2 inbound + 1 outbound (bob has no bot_response)
        await store.close()

    async def test_idempotent(self, memory_file, tmp_path):
        target = tmp_path / "bot.sqlite"
        await migrate(memory_file, target, dry_run=False)
        await migrate(memory_file, target, dry_run=False)
        store = ConversationStore(target)
        await store.init()
        ch = Channel(provider="discord", provider_channel_id="1234", name="geral", scope=ChannelScope.GROUP)
        msgs = await store.get_recent_messages("discord", ch, limit=100)
        # Same number after second run (UNIQUE constraint)
        assert len(msgs) == 3
        await store.close()
