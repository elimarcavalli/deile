"""Wire-contract tests for ``DispatchPayload.preferred_reasoning``.

The pipeline resolves a per-stage reasoning level and threads it into the
dispatch payload; the worker reads it back. This locks the wire format:
- builder includes the field only when set (exclude_none discipline);
- validator accepts known levels (lowercased) and rejects junk.
"""

from __future__ import annotations

import pytest

from deile.infrastructure.deile_worker_client import (
    DispatchPayload,
    WorkerDispatchError,
    build_dispatch_payload,
    validate_dispatch_payload,
)


@pytest.mark.unit
def test_builder_includes_reasoning_when_set():
    p = build_dispatch_payload(
        brief="x", channel_id="c", preferred_reasoning="ultracode"
    )
    assert p["preferred_reasoning"] == "ultracode"


@pytest.mark.unit
def test_builder_omits_reasoning_when_absent():
    p = build_dispatch_payload(brief="x", channel_id="c")
    assert "preferred_reasoning" not in p


@pytest.mark.unit
def test_payload_validates_known_level():
    vp = DispatchPayload(brief="x", channel_id="c", preferred_reasoning="HIGH")
    assert vp.preferred_reasoning == "high"  # normalized


@pytest.mark.unit
def test_payload_empty_collapses_to_none():
    vp = DispatchPayload(brief="x", channel_id="c", preferred_reasoning="   ")
    assert vp.preferred_reasoning is None


@pytest.mark.unit
def test_payload_rejects_unknown_level():
    with pytest.raises(WorkerDispatchError) as exc:
        validate_dispatch_payload(
            {"brief": "x", "channel_id": "c", "preferred_reasoning": "bogus"}
        )
    assert exc.value.error_code == "BAD_REQUEST"


@pytest.mark.unit
def test_roundtrip_model_dump_excludes_none():
    vp = DispatchPayload(brief="x", channel_id="c")
    body = vp.model_dump(exclude_none=True)
    assert "preferred_reasoning" not in body
