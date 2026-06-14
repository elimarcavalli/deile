"""Regression test for EventBus.publish() dead-letter path on queue full.

Bug: ``publish()`` used ``await queue.put(event)`` which blocks indefinitely
when the queue is at capacity — ``asyncio.QueueFull`` is only raised by
``put_nowait()``, making the dead-letter fallback unreachable code.

Fix: changed to ``queue.put_nowait(event)`` so ``QueueFull`` is raised and
the dead-letter path executes correctly.
"""

from __future__ import annotations

import asyncio

import pytest

from deile.events.event_bus import Event, EventBus, EventPriority, EventType


async def test_publish_queue_full_returns_false_and_routes_to_dead_letter() -> None:
    """Second publish on a saturated queue must return False quickly (no hang)
    and deposit the event in the dead-letter queue with reason 'Queue full'."""
    bus = EventBus(max_queue_size=1)

    # Wire the bus as running without real workers so nothing drains the queue.
    bus._running = True

    ev1 = Event(event_type=EventType.TASK_CREATED, source="test", priority=EventPriority.NORMAL)
    ev2 = Event(event_type=EventType.TASK_STARTED, source="test", priority=EventPriority.NORMAL)

    # Fill the NORMAL queue to capacity.
    ok1 = await asyncio.wait_for(bus.publish(ev1), timeout=1)
    assert ok1 is True, "first publish on empty queue must succeed"

    # Second publish must not block — use wait_for to prove it.
    ok2 = await asyncio.wait_for(bus.publish(ev2), timeout=1)
    assert ok2 is False, "publish on full queue must return False"

    dead_letters = await bus.get_dead_letters()
    assert len(dead_letters) == 1, "exactly one event should be in dead-letter"
    assert dead_letters[0].event_id == ev2.event_id
    assert dead_letters[0].metadata.get("dead_letter_reason") == "Queue full"
