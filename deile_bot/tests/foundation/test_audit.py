"""Tests for BotAuditLogger."""

from __future__ import annotations

import pytest

from deile_bot._testing import make_channel, make_user
from deile_bot.foundation.audit import AuditEventType, BotAuditLogger
from deile_bot.foundation.conversation_store import ConversationStore


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "audit.sqlite")
    await s.init()
    yield s
    await s.close()


class TestBotAuditLogger:
    async def test_log_persists_to_store(self, store):
        logger = BotAuditLogger(store)
        u = make_user()
        await logger.log(
            AuditEventType.AGENT_INVOKED,
            user=u,
            channel=make_channel(provider="discord"),
            payload={"persona": "developer"},
        )
        rows = await store.query_audit(event_type="agent_invoked")
        assert len(rows) == 1
        assert rows[0]["bot_user_id"] == u.bot_user_id
        assert rows[0]["payload"]["persona"] == "developer"

    async def test_log_minimal_no_user(self, store):
        logger = BotAuditLogger(store)
        await logger.log(AuditEventType.RATE_LIMITED)
        rows = await store.query_audit()
        assert any(r["event_type"] == "rate_limited" for r in rows)
