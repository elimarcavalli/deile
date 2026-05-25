"""Regression tests for ``EventBus.unsubscribe_all``.

Bug: there was no way to remove a wildcard handler registered via
``subscribe_all``. Per-dispatch callers (notably
``worker_server._run_task``) added a fresh closure on every invocation
and never cleaned up — ``_wildcard_handlers`` grew unbounded, every
event payed O(N) extra handler invocations, and dead closures retained
strong references to the dispatch's local state (memory leak).

This adds an ``unsubscribe_all`` API and verifies it removes the handler.
"""

from __future__ import annotations

import pytest

from deile.events.event_bus import Event, EventBus, EventType


async def test_unsubscribe_all_removes_handler() -> None:
    bus = EventBus()
    invocations: list[Event] = []

    async def handler(evt: Event) -> None:
        invocations.append(evt)

    bus.subscribe_all(handler)
    assert handler in bus._wildcard_handlers

    removed = bus.unsubscribe_all(handler)
    assert removed is True
    assert handler not in bus._wildcard_handlers


async def test_unsubscribe_all_returns_false_when_not_registered() -> None:
    bus = EventBus()

    async def stranger(evt: Event) -> None:
        pass

    assert bus.unsubscribe_all(stranger) is False


async def test_unsubscribe_all_does_not_remove_other_handlers() -> None:
    bus = EventBus()

    async def h1(evt: Event) -> None:
        pass

    async def h2(evt: Event) -> None:
        pass

    bus.subscribe_all(h1)
    bus.subscribe_all(h2)
    assert bus.unsubscribe_all(h1) is True
    assert h1 not in bus._wildcard_handlers
    assert h2 in bus._wildcard_handlers


async def test_unsubscribed_handler_does_not_fire() -> None:
    bus = EventBus()
    await bus.start()
    try:
        invoked = []

        async def handler(evt: Event) -> None:
            invoked.append(evt.event_id)

        bus.subscribe_all(handler)
        ok1 = await bus.publish_and_wait(
            Event(event_type=EventType.TASK_CREATED, source="t"), timeout=5
        )
        assert ok1
        assert len(invoked) == 1

        bus.unsubscribe_all(handler)

        ok2 = await bus.publish_and_wait(
            Event(event_type=EventType.TASK_CREATED, source="t"), timeout=5
        )
        assert ok2
        # Still 1 — second event went through but no longer hit our handler.
        assert len(invoked) == 1
    finally:
        await bus.stop()
