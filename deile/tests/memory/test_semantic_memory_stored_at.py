"""Regression tests for ``SemanticMemory.store_knowledge`` stored_at precedence.

Bug: store_knowledge() unconditionally overwrote stored_at with extracted_at,
destroying the stored_at that store_correction() had already calculated.

Fix: record.get('extracted_at', record.get('stored_at', 0)) preserves any
stored_at already present (correction flow) while still deriving from
extracted_at in the normal flow.
"""

from __future__ import annotations

from pathlib import Path

from deile.memory.semantic_memory import SemanticMemory


async def test_store_correction_preserves_stored_at(tmp_path: Path) -> None:
    """store_correction() sets stored_at via loop time; store_knowledge must not
    overwrite it with 0 (the extracted_at fallback) when extracted_at is absent."""
    sm = SemanticMemory(storage_dir=tmp_path)
    await sm.initialize()
    try:
        await sm.store_correction("id1", {"foo": "bar"})

        stored = sm._knowledge_base[-1]
        assert stored["stored_at"] > 0, (
            "stored_at should be the event-loop timestamp set by store_correction, "
            f"got {stored['stored_at']!r}"
        )
    finally:
        await sm.shutdown()


async def test_store_knowledge_with_extracted_at_derives_stored_at(
    tmp_path: Path,
) -> None:
    """Normal flow: a record that carries extracted_at should still derive
    stored_at from extracted_at (not lose it to the store_correction path)."""
    sm = SemanticMemory(storage_dir=tmp_path)
    await sm.initialize()
    try:
        await sm.store_knowledge({"topic": "embeddings", "extracted_at": 99999})

        stored = sm._knowledge_base[-1]
        assert (
            stored["stored_at"] == 99999
        ), f"stored_at should equal extracted_at (99999), got {stored['stored_at']!r}"
    finally:
        await sm.shutdown()
