"""Regression tests for WorkingMemory.update_with_feedback tag-index sync.

Bug: update_with_feedback added 'positive_feedback'/'negative_feedback' to
entry.tags but never updated _tag_index. search() filters candidates
exclusively via _tag_index, so searches by these tags always returned zero
results. Fix: after mutating entry.tags, call
self._tag_index.setdefault(tag, set()).add(entry_id).
"""

from __future__ import annotations

import pytest

from deile.memory.working_memory import WorkingMemory


@pytest.mark.parametrize(
    "feedback_type, feedback_tag, query",
    [
        ("positive", "positive_feedback", "hello positive world"),
        ("negative", "negative_feedback", "hello negative world"),
    ],
)
async def test_feedback_tag_searchable_excludes_untagged(
    feedback_type: str, feedback_tag: str, query: str
) -> None:
    """search by the feedback tag must return ONLY the entry given feedback.

    A control entry sharing the same searchable text but WITHOUT feedback must
    be excluded. search() filters candidates via _tag_index and SKIPS the tag
    filter when the tag is absent from the index — so with the bug (tag added to
    entry.tags but never to _tag_index) the filter is bypassed and the untagged
    control leaks in. Only correct _tag_index sync excludes it, making the index
    load-bearing for this assertion.
    """
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    entry_id = await wm.store(f"{query} (feedback)", entry_type="context")
    control_id = await wm.store(f"{query} (control)", entry_type="context")

    ok = await wm.update_with_feedback(entry_id, feedback_type, {})
    assert ok is True

    results = await wm.search(query, tags={feedback_tag})
    ids = [r["entry_id"] for r in results]

    assert entry_id in ids, (
        f"search by '{feedback_tag}' tag did not return the entry given feedback — "
        "_tag_index was not updated by update_with_feedback"
    )
    assert control_id not in ids, (
        f"search by '{feedback_tag}' tag returned an entry WITHOUT that feedback — "
        "the tag filter was skipped because _tag_index lacks the tag"
    )
    assert len(results) == 1


async def test_positive_feedback_tag_index_updated_directly() -> None:
    """_tag_index must contain the entry_id after positive feedback."""
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    entry_id = await wm.store("inspect index", entry_type="context")
    assert "positive_feedback" not in wm._tag_index

    await wm.update_with_feedback(entry_id, "positive", {})

    assert "positive_feedback" in wm._tag_index
    assert entry_id in wm._tag_index["positive_feedback"]


async def test_negative_feedback_tag_index_updated_directly() -> None:
    """_tag_index must contain the entry_id after negative feedback."""
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    entry_id = await wm.store("inspect negative index", entry_type="context")
    assert "negative_feedback" not in wm._tag_index

    await wm.update_with_feedback(entry_id, "negative", {})

    assert "negative_feedback" in wm._tag_index
    assert entry_id in wm._tag_index["negative_feedback"]


async def test_unknown_feedback_type_does_not_pollute_tag_index() -> None:
    """Unrecognised feedback types must not add spurious keys to _tag_index."""
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    entry_id = await wm.store("neutral content", entry_type="context")
    ok = await wm.update_with_feedback(entry_id, "neutral", {"score": 0.5})
    assert ok is True

    assert "neutral" not in wm._tag_index
    assert "positive_feedback" not in wm._tag_index
    assert "negative_feedback" not in wm._tag_index


async def test_update_with_feedback_returns_false_for_missing_entry() -> None:
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    ok = await wm.update_with_feedback("nonexistent_id", "positive", {})
    assert ok is False
