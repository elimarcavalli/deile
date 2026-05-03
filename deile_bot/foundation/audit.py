"""BotAuditLogger — wraps deile.security.audit_logger and persists to SQLite."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional

from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import BotUser, Channel
from deile_bot.foundation.logging import get_logger


class AuditEventType(str, Enum):
    INBOUND_RECEIVED = "inbound_received"
    SHOULD_RESPOND_DECIDED = "should_respond_decided"
    AGENT_INVOKED = "agent_invoked"
    AGENT_RESPONDED = "agent_responded"
    AGENT_FAILED = "agent_failed"
    OUTBOUND_SENT = "outbound_sent"
    OUTBOUND_FAILED = "outbound_failed"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    DLQ_ENQUEUED = "dlq_enqueued"
    DLQ_REPLAYED = "dlq_replayed"


class BotAuditLogger:
    def __init__(
        self,
        store: ConversationStore,
        deile_audit: Optional[Any] = None,
    ):
        self._store = store
        self._deile_audit = deile_audit
        self._logger = get_logger("audit")

    async def log(
        self,
        event_type: AuditEventType,
        *,
        user: Optional[BotUser] = None,
        channel: Optional[Channel] = None,
        message_id: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        bot_user_id = user.bot_user_id if user else None
        provider = (channel.provider if channel else None) or (user.provider if user else None)
        provider_channel_id = channel.provider_channel_id if channel else None
        await self._store.insert_audit(
            event_type=event_type.value,
            bot_user_id=bot_user_id,
            provider=provider,
            provider_channel_id=provider_channel_id,
            provider_message_id=message_id,
            payload=payload,
        )
        self._logger.info(
            f"audit:{event_type.value}",
            extra={
                "event": event_type.value,
                "bot_user_id": bot_user_id,
                "provider": provider,
                "channel_id": provider_channel_id,
                "message_id": message_id,
                "payload": dict(payload) if payload else None,
            },
        )
