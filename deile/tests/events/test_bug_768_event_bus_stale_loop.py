"""xfail test for bug #768: EventBus queues bound to closed event loop.

Bug: asyncio.Queue objects are created in EventBus.__init__ and bind to
the running event loop at construction time (Python 3.10+ _LoopBoundMixin).
When the same EventBus singleton is reused across multiple asyncio.run()
invocations, workers in the second run call queue.get() and receive
RuntimeError("is bound to a different event loop"), entering an infinite
retry loop. Events are silently never processed.

Fix: Recreate queues in EventBus.start() when bound to a closed/different loop.
Tracker: #768
"""

from __future__ import annotations

import asyncio

import pytest

from deile.events.event_bus import EventBus, Event, EventType


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 event-bus-stale-loop — fix pending tracker #768",
)
def test_event_bus_processes_events_after_second_asyncio_run() -> None:
    """EventBus must process events when reused across two asyncio.run() calls.

    When the bug is present:
      - Workers raise RuntimeError (bound to different loop) on every queue.get()
      - processed_count remains 0 despite successful publish()

    When fixed:
      - Queues are recreated in start(); workers process events normally
    """
    bus = EventBus()
    processed_count_run1 = [0]
    processed_count_run2 = [0]

    async def handler_run1(event: Event) -> None:
        processed_count_run1[0] += 1

    async def handler_run2(event: Event) -> None:
        processed_count_run2[0] += 1

    async def _run1() -> None:
        await bus.start()
        bus.subscribe(EventType.TASK_CREATED, handler_run1)
        event = Event(event_type=EventType.TASK_CREATED, source="run1")
        bus.publish(event)
        # Give workers time to process
        await asyncio.sleep(0.1)
        await bus.stop()

    async def _run2() -> None:
        await bus.start()
        bus.subscribe(EventType.TASK_CREATED, handler_run2)
        event = Event(event_type=EventType.TASK_CREATED, source="run2")
        bus.publish(event)
        await asyncio.sleep(0.3)
        await bus.stop()

    asyncio.run(_run1())
    # Do NOT call reset_event_bus() — we want the stale-loop scenario
    asyncio.run(_run2())

    assert processed_count_run2[0] > 0, (
        f"EventBus processed {processed_count_run2[0]} events in the second "
        "asyncio.run() — expected > 0. "
        "Workers are stuck in RuntimeError retry loop (stale queue loop)."
    )
