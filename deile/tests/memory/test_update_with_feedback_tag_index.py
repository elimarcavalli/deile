"""Regression tests for WorkingMemory.update_with_feedback tag-index sync.

Bug: update_with_feedback added 'positive_feedback'/'negative_feedback' to
entry.tags but never updated _tag_index. search() filters candidates
exclusively via _tag_index, so searches by these tags always returned zero
results. Fix: after mutating entry.tags, call
self._tag_index.setdefault(tag, set()).add(entry_id).
"""

from __future__ import annotations

from deile.memory.working_memory import WorkingMemory


async def test_positive_feedback_tag_searchable() -> None:
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    entry_id = await wm.store("hello positive world", entry_type="context")
    ok = await wm.update_with_feedback(entry_id, "positive", {})
    assert ok is True

    results = await wm.search("hello positive world", tags={"positive_feedback"})
    assert len(results) >= 1, (
        "search by 'positive_feedback' tag returned no results — "
        "_tag_index was not updated by update_with_feedback"
    )
    ids = [r["entry_id"] for r in results]
    assert entry_id in ids


async def test_negative_feedback_tag_searchable() -> None:
    wm = WorkingMemory(max_size=100_000, ttl=3600)
    wm._is_initialized = True

    entry_id = await wm.store("hello negative world", entry_type="context")
    ok = await wm.update_with_feedback(entry_id, "negative", {})
    assert ok is True

    results = await wm.search("hello negative world", tags={"negative_feedback"})
    assert len(results) >= 1, (
        "search by 'negative_feedback' tag returned no results — "
        "_tag_index was not updated by update_with_feedback"
    )
    ids = [r["entry_id"] for r in results]
    assert entry_id in ids


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
