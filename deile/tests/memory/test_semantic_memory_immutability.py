"""Regression test for ``SemanticMemory.store_knowledge`` mutating its arg.

Bug: the function did ``knowledge['stored_at'] = ...`` directly on the
caller's dict. Callers that continued iterating or serializing the dict
post-call saw an unexpected ``stored_at`` key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.memory.semantic_memory import SemanticMemory


async def test_store_knowledge_does_not_mutate_caller_dict(tmp_path: Path) -> None:
    sm = SemanticMemory(storage_dir=tmp_path)
    await sm.initialize()
    try:
        caller_dict = {"topic": "auth", "extracted_at": 12345}
        snapshot = dict(caller_dict)

        await sm.store_knowledge(caller_dict)

        # Caller's dict is unchanged.
        assert caller_dict == snapshot

        # But the stored record HAS the stored_at field.
        assert any(entry.get("stored_at") == 12345 for entry in sm._knowledge_base)
    finally:
        await sm.shutdown()


async def test_store_correction_does_not_mutate_correction_data(tmp_path: Path) -> None:
    sm = SemanticMemory(storage_dir=tmp_path)
    await sm.initialize()
    try:
        correction_data = {"old": "x", "new": "y"}
        snapshot = dict(correction_data)
        await sm.store_correction("interaction-1", correction_data)
        assert correction_data == snapshot
    finally:
        await sm.shutdown()
