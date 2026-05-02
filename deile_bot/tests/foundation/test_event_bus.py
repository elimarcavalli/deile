"""Tests for BotEventBus wrapper."""

from __future__ import annotations

from deile_bot.foundation.event_bus import BotEventBus, BotEventType


class FakeBus:
    def __init__(self):
        self.published = []

    def publish(self, name, payload):
        self.published.append((name, payload))


class AsyncFakeBus:
    def __init__(self):
        self.published = []

    async def publish(self, name, payload):
        self.published.append((name, payload))


class TestPublish:
    async def test_sync_inner_bus(self):
        inner = FakeBus()
        bus = BotEventBus(inner)
        await bus.publish(BotEventType.AGENT_INVOKED, {"x": 1})
        assert inner.published[0][0] == "bot.agent.invoked"

    async def test_async_inner_bus(self):
        inner = AsyncFakeBus()
        bus = BotEventBus(inner)
        await bus.publish(BotEventType.OUTBOUND_SENT, {"x": 2})
        assert inner.published[0][0] == "bot.outbound.sent"

    async def test_subscribe(self):
        bus = BotEventBus()
        captured = []

        async def cb(payload):
            captured.append(payload)

        bus.subscribe(BotEventType.AGENT_RESPONDED, cb)
        await bus.publish(BotEventType.AGENT_RESPONDED, {"k": 1})
        assert captured == [{"k": 1}]

    async def test_no_inner_bus_ok(self):
        bus = BotEventBus(None)
        await bus.publish(BotEventType.RATE_LIMITED, {"reason": "user_burst"})
