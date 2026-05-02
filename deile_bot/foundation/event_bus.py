"""BotEventBus — wraps deile.events.event_bus with bot-prefixed event types."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional

from deile_bot.foundation.logging import get_logger


class BotEventType(str, Enum):
    INBOUND_RECEIVED = "bot.inbound.received"
    SHOULD_RESPOND_DECIDED = "bot.intent.decided"
    AGENT_INVOKED = "bot.agent.invoked"
    AGENT_RESPONDED = "bot.agent.responded"
    AGENT_FAILED = "bot.agent.failed"
    OUTBOUND_SENT = "bot.outbound.sent"
    OUTBOUND_FAILED = "bot.outbound.failed"
    PERMISSION_DENIED = "bot.permission.denied"
    RATE_LIMITED = "bot.rate.limited"
    DLQ_ENQUEUED = "bot.dlq.enqueued"
    DLQ_REPLAYED = "bot.dlq.replayed"
    CONFIG_RELOADED = "bot.config.reloaded"


class BotEventBus:
    def __init__(self, deile_bus: Optional[Any] = None):
        self._bus = deile_bus
        self._subscribers = {}
        self._logger = get_logger("event_bus")

    async def publish(self, event_type: BotEventType, payload: Mapping[str, Any]) -> None:
        if self._bus is not None:
            try:
                if hasattr(self._bus, "publish"):
                    res = self._bus.publish(event_type.value, dict(payload))
                    if hasattr(res, "__await__"):
                        await res
                elif hasattr(self._bus, "emit"):
                    res = self._bus.emit(event_type.value, dict(payload))
                    if hasattr(res, "__await__"):
                        await res
            except Exception:
                self._logger.exception("event_bus publish failed")
        for cb in self._subscribers.get(event_type, []):
            try:
                res = cb(payload)
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                self._logger.exception("subscriber failed")

    def subscribe(self, event_type: BotEventType, callback) -> None:
        self._subscribers.setdefault(event_type, []).append(callback)
