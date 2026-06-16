"""Tests for the temporal cascade helpers (cascade_stream / cascade_until)."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, List

import pytest

from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.ui.stage_cascade import cascade_stream, cascade_until

pytestmark = pytest.mark.asyncio


# ────────────────────────────────────────────────────────────────────────
# cascade_stream
# ────────────────────────────────────────────────────────────────────────


async def _empty_source() -> AsyncIterator[UnifiedStreamEvent]:
    if False:  # pragma: no cover - intentional empty async generator
        yield  # type: ignore[unreachable]


async def _source_one(text: str) -> AsyncIterator[UnifiedStreamEvent]:
    yield UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=text)


async def test_cascade_stream_emits_initial_then_forwards_source() -> None:
    out: List[UnifiedStreamEvent] = []
    async for ev in cascade_stream(
        _source_one("hello"),
        message_key="parse_input",
    ):
        out.append(ev)

    assert len(out) >= 2
    assert out[0].type is StreamEventType.STAGE
    assert out[0].stage and "Interpretando" in out[0].stage
    assert any(e.type is StreamEventType.TEXT_DELTA and e.text == "hello" for e in out)


async def test_cascade_stream_unknown_key_still_yields_initial_label() -> None:
    """Unknown scenario keys fall back to the key text, but the helper does
    not blow up — it simply forwards source events after the initial label."""
    out: List[UnifiedStreamEvent] = []
    async for ev in cascade_stream(
        _source_one("payload"),
        message_key="__nonexistent_scenario__",
    ):
        out.append(ev)

    assert out[0].type is StreamEventType.STAGE
    assert any(e.text == "payload" for e in out if e.type is StreamEventType.TEXT_DELTA)


async def test_cascade_stream_stamps_event_iteration_on_stage_events() -> None:
    out: List[UnifiedStreamEvent] = []
    async for ev in cascade_stream(
        _source_one("z"),
        message_key="parse_input",
        event_iteration=7,
    ):
        out.append(ev)

    initial_stage = next(e for e in out if e.type is StreamEventType.STAGE)
    assert initial_stage.iteration == 7


async def test_cascade_stream_kwarg_iteration_does_not_collide_with_message_ctx() -> (
    None
):
    """Reproduce the kwarg collision bug: callers pass ``iteration`` in the
    message-format ctx (e.g. for ``await_next_response``'s {iteration}
    placeholder), and the helper's own loop-iteration field must not clash.
    """
    out: List[UnifiedStreamEvent] = []
    async for ev in cascade_stream(
        _source_one("payload"),
        message_key="await_next_response",
        event_iteration=2,
        iteration="3",  # message-format placeholder
    ):
        out.append(ev)

    assert any(e.type is StreamEventType.STAGE and "3" in (e.stage or "") for e in out)


# ────────────────────────────────────────────────────────────────────────
# cascade_until
# ────────────────────────────────────────────────────────────────────────


async def test_cascade_until_yields_initial_stage_then_result_tuple() -> None:
    async def _work() -> str:
        await asyncio.sleep(0)
        return "ok"

    items = []
    async for item in cascade_until(_work(), message_key="validation_retry"):
        items.append(item)

    assert isinstance(items[0], UnifiedStreamEvent)
    assert items[0].type is StreamEventType.STAGE
    assert items[-1] == ("result", "ok")


async def test_cascade_until_propagates_underlying_exception() -> None:
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        async for _ in cascade_until(_boom(), message_key="validation_retry"):
            pass


async def test_cascade_until_works_with_unknown_message_key() -> None:
    async def _work() -> int:
        return 42

    items = []
    async for item in cascade_until(_work(), message_key="__nope__"):
        items.append(item)

    assert items[-1] == ("result", 42)
