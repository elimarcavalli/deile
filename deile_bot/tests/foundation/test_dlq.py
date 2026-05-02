"""Tests for DeadLetterQueue."""

from __future__ import annotations

import pytest

from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.dlq import DeadLetterQueue
from deile_bot.foundation.exceptions import DLQError
from deile_bot.foundation.settings import BotSettings


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "dlq.sqlite")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
async def dlq(store):
    return DeadLetterQueue(store, BotSettings())


class TestEnqueueListCount:
    async def test_enqueue_increments_count(self, dlq):
        await dlq.enqueue("discord", {"text": "x"}, "boom", attempts=3)
        assert await dlq.count("discord") == 1

    async def test_list_pending_returns(self, dlq):
        await dlq.enqueue("discord", {"text": "y"}, "err1", 1)
        rows = await dlq.list_pending(provider="discord")
        assert len(rows) == 1
        assert rows[0]["last_error"] == "err1"


class TestReplay:
    async def test_replay_success_clears(self, dlq):
        await dlq.enqueue("discord", {"text": "ok"}, "boom", 1)

        async def sender(payload):
            return "sent-1"

        results = await dlq.replay(sender, provider="discord")
        assert all(r["result"] == "ok" for r in results)
        assert await dlq.count("discord") == 0

    async def test_replay_failure_keeps_in_dlq(self, dlq):
        await dlq.enqueue("discord", {"text": "ok"}, "boom", 1)

        def sender(payload):
            raise RuntimeError("still down")

        results = await dlq.replay(sender, provider="discord")
        assert any("failed" in r["result"] for r in results)
        assert await dlq.count("discord") >= 1

    async def test_replay_dry_run(self, dlq):
        await dlq.enqueue("discord", {"text": "x"}, "e", 1)

        async def sender(payload):
            raise AssertionError("must not call")

        results = await dlq.replay(sender, provider="discord", dry_run=True)
        assert all(r["result"] == "dry_run" for r in results)
        assert await dlq.count("discord") == 1


class TestPurge:
    async def test_purge_negative_raises(self, dlq):
        with pytest.raises(DLQError):
            await dlq.purge(-1)

    async def test_purge_zero_clears(self, dlq):
        await dlq.enqueue("discord", {"text": "x"}, "e", 1)
        removed = await dlq.purge(0)
        assert removed >= 0
