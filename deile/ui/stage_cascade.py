"""Temporal cascade helpers for long-running STAGE feedback.

Two flavors are exposed:

* ``StageCascade`` — async context manager that fires the cascade through a
  caller-supplied ``send_stage`` callback. Useful when the consumer can react
  to a callback (queues, event buses).

* ``cascade_stream(source, message_key, ...)`` — async generator that yields
  the *initial* STAGE event immediately, then interleaves cascade STAGEs (at
  3s/10s/30s) with events from ``source`` until ``source`` is exhausted.
  Used by ``ToolLoopExecutor`` to dress the model's first-token wait with
  evolving feedback.

* ``cascade_until(coro, message_key, ...)`` — async generator that yields the
  initial STAGE immediately, then advances the cascade while ``coro`` runs,
  and finally yields ``("result", value)`` once ``coro`` completes. Used in
  ``DeileAgent`` to surface evolution during the validation-gate retry, which
  is a single non-streaming awaitable.

The deterministic temporal cascade follows the issue contract:

  - ``initial``   — emitted immediately
  - ``after_3s``  — emitted after 3 seconds (reinforcement)
  - ``after_10s`` — emitted after 10 seconds (acknowledgement)
  - ``after_30s`` — emitted after 30 seconds (assume real delay)

Spinner text never changes before 3 seconds — this prevents the classic
"label flicker" where rapid phase changes produce an unreadable blur.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Tuple

from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.ui.stage_messages import get_stage_message, has_stage_messages

logger = logging.getLogger(__name__)

StageSender = Callable[[str], Awaitable[Any]]
"""Callback signature: ``async def send_stage(label: str) -> None``."""

_CASCADE_LEVELS: Tuple[Tuple[float, str], ...] = (
    (3.0, "after_3s"),
    (10.0, "after_10s"),
    (30.0, "after_30s"),
)


class StageCascade:
    """Async context manager that advances STAGE labels on a time cascade.

    Args:
        send_stage: Awaitable callback — receives the label text.
        message_key: Scenario key in ``stage_messages.STAGE_MESSAGES``.
        min_interval: Minimum seconds between label changes (default 3s).
        **ctx: Format-string context passed to ``get_stage_message``.
    """

    def __init__(
        self,
        send_stage: StageSender,
        message_key: str,
        min_interval: float = 3.0,
        **ctx: Any,
    ) -> None:
        self._send_stage = send_stage
        self._key = message_key
        self._ctx = dict(ctx)
        self._min_interval = float(min_interval)
        self._start_time: float = 0.0
        self._last_label: Optional[str] = None
        self._scheduled: list[asyncio.Task[Any]] = []

    async def __aenter__(self) -> "StageCascade":
        self._start_time = asyncio.get_event_loop().time()
        label = get_stage_message(self._key, "initial", **self._ctx)
        self._last_label = label
        await self._send_stage(label)
        for delay, level in _CASCADE_LEVELS:
            self._spawn_timer(delay, level, label)
        return self

    async def __aexit__(self, *args: Any) -> None:
        for t in self._scheduled:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._scheduled.clear()

    def _spawn_timer(self, delay: float, level: str, fallback_label: str) -> None:
        if not has_stage_messages(self._key):
            return
        candidate = get_stage_message(self._key, level, **self._ctx)
        if candidate == fallback_label:
            return

        async def _fire() -> None:
            try:
                await asyncio.sleep(delay)
                label = get_stage_message(self._key, level, **self._ctx)
                if label != self._last_label:
                    self._last_label = label
                    await self._send_stage(label)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("StageCascade timer %s failed", level, exc_info=True)

        self._scheduled.append(asyncio.create_task(_fire()))

    @property
    def elapsed(self) -> float:
        """Seconds since the cascade started."""
        if self._start_time == 0.0:
            return 0.0
        return asyncio.get_event_loop().time() - self._start_time


def _stage_event(label: str, iteration: Optional[int] = None) -> UnifiedStreamEvent:
    return UnifiedStreamEvent(
        type=StreamEventType.STAGE, stage=label, iteration=iteration
    )


async def cascade_stream(
    source: AsyncIterator[UnifiedStreamEvent],
    *,
    message_key: str,
    event_iteration: Optional[int] = None,
    **ctx: Any,
) -> AsyncIterator[UnifiedStreamEvent]:
    """Yield ``message_key`` cascade STAGEs interleaved with ``source`` events.

    The cascade emits the ``initial`` label immediately, then at 3s/10s/30s
    upgrades the label as long as ``source`` has not yielded any non-STAGE
    event. Once ``source`` produces something, the cascade keeps firing in the
    background but its labels are still forwarded — so phase changes like
    "<provider> formulando resposta..." can land even while text already
    started streaming.

    All scheduled timers are cancelled when ``source`` is exhausted.
    """
    yield _stage_event(
        get_stage_message(message_key, "initial", **ctx), event_iteration
    )

    if not has_stage_messages(message_key):
        async for ev in source:
            yield ev
        return

    pending: asyncio.Queue[str] = asyncio.Queue()
    timer_tasks: list[asyncio.Task[Any]] = []

    def _spawn(delay: float, level: str) -> None:
        async def _fire() -> None:
            try:
                await asyncio.sleep(delay)
                label = get_stage_message(message_key, level, **ctx)
                await pending.put(label)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("cascade_stream timer %s failed", level, exc_info=True)

        timer_tasks.append(asyncio.create_task(_fire()))

    initial_label = get_stage_message(message_key, "initial", **ctx)
    for delay, level in _CASCADE_LEVELS:
        candidate = get_stage_message(message_key, level, **ctx)
        if candidate != initial_label:
            _spawn(delay, level)

    source_iter = source.__aiter__()
    next_source: Optional[asyncio.Task[Any]] = asyncio.create_task(
        source_iter.__anext__()
    )
    next_stage: Optional[asyncio.Task[Any]] = asyncio.create_task(pending.get())

    try:
        while next_source is not None:
            wait_set = {t for t in (next_source, next_stage) if t is not None}
            done, _ = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED
            )

            if next_stage is not None and next_stage in done:
                try:
                    label = next_stage.result()
                    yield _stage_event(label, event_iteration)
                except asyncio.CancelledError:
                    pass
                next_stage = asyncio.create_task(pending.get())

            if next_source in done:
                try:
                    ev = next_source.result()
                    yield ev
                    next_source = asyncio.create_task(source_iter.__anext__())
                except StopAsyncIteration:
                    next_source = None
                    break
    finally:
        for t in timer_tasks:
            if not t.done():
                t.cancel()
        if next_source is not None and not next_source.done():
            next_source.cancel()
        if next_stage is not None and not next_stage.done():
            next_stage.cancel()


async def cascade_until(
    coro: Awaitable[Any],
    *,
    message_key: str,
    event_iteration: Optional[int] = None,
    **ctx: Any,
) -> AsyncIterator[Any]:
    """Yield cascade STAGE events while ``coro`` runs, then yield the result.

    Yields:
        ``UnifiedStreamEvent`` instances for each cascade STAGE, then a final
        2-tuple ``("result", value)`` carrying the awaited return value. If
        ``coro`` raises, the exception is re-raised after timer cleanup.

    Pattern:

        async for item in cascade_until(some_awaitable(), message_key="x"):
            if isinstance(item, tuple) and item[0] == "result":
                final = item[1]
            else:
                yield item   # forward STAGE upstream
    """
    yield _stage_event(
        get_stage_message(message_key, "initial", **ctx), event_iteration
    )

    initial_label = get_stage_message(message_key, "initial", **ctx)
    pending: asyncio.Queue[str] = asyncio.Queue()
    timer_tasks: list[asyncio.Task[Any]] = []

    def _spawn(delay: float, level: str) -> None:
        async def _fire() -> None:
            try:
                await asyncio.sleep(delay)
                label = get_stage_message(message_key, level, **ctx)
                await pending.put(label)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("cascade_until timer %s failed", level, exc_info=True)

        timer_tasks.append(asyncio.create_task(_fire()))

    if has_stage_messages(message_key):
        for delay, level in _CASCADE_LEVELS:
            candidate = get_stage_message(message_key, level, **ctx)
            if candidate != initial_label:
                _spawn(delay, level)

    work_task: asyncio.Task[Any] = asyncio.ensure_future(coro)
    next_stage: Optional[asyncio.Task[Any]] = asyncio.create_task(pending.get())

    try:
        while not work_task.done():
            wait_set = {t for t in (work_task, next_stage) if t is not None}
            done, _ = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED
            )
            if next_stage is not None and next_stage in done:
                try:
                    label = next_stage.result()
                    yield _stage_event(label, event_iteration)
                except asyncio.CancelledError:
                    pass
                next_stage = asyncio.create_task(pending.get())
            if work_task in done:
                break

        result = await work_task
        yield ("result", result)
    finally:
        for t in timer_tasks:
            if not t.done():
                t.cancel()
        if next_stage is not None and not next_stage.done():
            next_stage.cancel()
        if not work_task.done():
            work_task.cancel()
