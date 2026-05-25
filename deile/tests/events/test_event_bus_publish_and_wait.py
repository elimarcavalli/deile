"""Regression tests for ``EventBus.publish_and_wait`` synchronization.

Bug: ``_is_event_processed`` was a stub that always returned ``True``.
``publish_and_wait`` would therefore return ``True`` on the very first
iteration of its polling loop — before any worker had even pulled the
event off the queue. Any caller relying on the return value as a
"processing completed" signal was racing against an event that might
still be pending or about to fail.

Fix: ``EventBus`` now records every event_id whose ``_process_event``
has finished (success OR dead-letter); ``_is_event_processed`` returns
True only after that point. The tracker is bounded by a deque to avoid
unbounded growth.
"""

from __future__ import annotations

import asyncio

from deile.events.event_bus import Event, EventBus, EventType


async def test_publish_and_wait_returns_true_only_after_handler_runs() -> None:
    bus = EventBus()
    await bus.start()
    try:
        handler_started = asyncio.Event()
        handler_finished = asyncio.Event()

        async def slow_handler(event: Event) -> None:
            handler_started.set()
            await asyncio.sleep(0.2)
            handler_finished.set()

        bus.subscribe(EventType.TASK_CREATED, slow_handler)

        event = Event(event_type=EventType.TASK_CREATED, source="test")
        # publish_and_wait must NOT return until slow_handler finishes.
        result = await bus.publish_and_wait(event, timeout=5.0)

        assert result is True
        assert handler_started.is_set()
        assert handler_finished.is_set()
    finally:
        await bus.stop()


async def test_publish_and_wait_with_no_handlers_still_marks_processed() -> None:
    """Even when there are no handlers, ``_process_event`` runs to completion;
    publish_and_wait must observe that."""
    bus = EventBus()
    await bus.start()
    try:
        event = Event(event_type=EventType.TASK_CREATED, source="test")
        result = await bus.publish_and_wait(event, timeout=5.0)
        assert result is True
    finally:
        await bus.stop()


async def test_publish_and_wait_times_out_when_bus_not_running() -> None:
    """publish() on a stopped bus returns False; publish_and_wait short-circuits."""
    bus = EventBus()
    # Don't start.
    event = Event(event_type=EventType.TASK_CREATED, source="test")
    result = await bus.publish_and_wait(event, timeout=0.5)
    assert result is False


async def test_processed_tracker_is_bounded() -> None:
    """Long runs must not leak memory via the processed-id tracker."""
    bus = EventBus()
    # Manually populate to verify FIFO eviction.
    maxlen = bus._processed_event_ids.maxlen
    assert maxlen and maxlen > 0
    for i in range(maxlen + 50):
        bus._mark_event_processed(f"ev-{i}")
    assert len(bus._processed_event_ids) == maxlen
    # Earliest IDs should have been evicted from the lookup set too.
    assert "ev-0" not in bus._processed_lookup
    assert f"ev-{maxlen + 49}" in bus._processed_lookup


async def test_mark_event_processed_is_idempotent() -> None:
    bus = EventBus()
    bus._mark_event_processed("dup")
    bus._mark_event_processed("dup")
    assert list(bus._processed_event_ids).count("dup") == 1
