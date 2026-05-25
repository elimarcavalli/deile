"""Wire-format tests for ``DispatchPayload.preferred_model`` (issue #305).

Covers the Pydantic validator on the bot side (`_validate_model_slug`) and
the builder helper (`build_dispatch_payload`). The validator is the wire
boundary: a typo here only manifests as a 5xx many minutes later on the
worker side, so we must reject fast and precisely.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deile.infrastructure.deile_worker_client import (DispatchPayload,
                                                      build_dispatch_payload)


class TestPreferredModelValidator:
    def test_accepts_valid_slug(self):
        p = DispatchPayload(
            brief="x", channel_id="c",
            preferred_model="deepseek:deepseek-v4-pro",
        )
        assert p.preferred_model == "deepseek:deepseek-v4-pro"

    def test_none_stays_none(self):
        p = DispatchPayload(brief="x", channel_id="c", preferred_model=None)
        assert p.preferred_model is None

    def test_default_is_none(self):
        # Absence == None — no implicit fallback, the builder decides whether
        # to drop the key entirely from the wire payload.
        p = DispatchPayload(brief="x", channel_id="c")
        assert p.preferred_model is None

    def test_empty_and_whitespace_collapse_to_none(self):
        assert DispatchPayload(brief="x", channel_id="c",
                               preferred_model="").preferred_model is None
        assert DispatchPayload(brief="x", channel_id="c",
                               preferred_model="   ").preferred_model is None

    def test_strips_surrounding_whitespace(self):
        p = DispatchPayload(brief="x", channel_id="c",
                            preferred_model="  deepseek:v4-pro  ")
        assert p.preferred_model == "deepseek:v4-pro"

    @pytest.mark.parametrize("bad", [
        "garbage",
        "ANTHROPIC:opus",
        "provider:Model",
        ":model",
        "provider:",
        "provider:with space",
    ])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValidationError):
            DispatchPayload(brief="x", channel_id="c", preferred_model=bad)

    def test_enforces_max_length(self):
        # 128 chars is the field cap (defends against an LLM emitting an
        # accidental novel as the slug). Validator only runs if length passes.
        with pytest.raises(ValidationError):
            DispatchPayload(brief="x", channel_id="c",
                            preferred_model="a:" + "b" * 200)


class TestBuildDispatchPayload:
    def test_adds_preferred_model_when_truthy(self):
        p = build_dispatch_payload(
            brief="x", channel_id="c",
            preferred_model="anthropic:claude-opus-4-7",
        )
        assert p["preferred_model"] == "anthropic:claude-opus-4-7"

    def test_omits_preferred_model_when_none(self):
        # Drop the key so ``model_dump(exclude_none=True)`` keeps the payload
        # minimal AND the worker falls back to its own default cleanly.
        p = build_dispatch_payload(brief="x", channel_id="c",
                                   preferred_model=None)
        assert "preferred_model" not in p

    def test_omits_preferred_model_when_default(self):
        # Default kwarg None — same as explicitly passing None.
        p = build_dispatch_payload(brief="x", channel_id="c")
        assert "preferred_model" not in p

    def test_omits_when_empty_string(self):
        # Builder uses ``if preferred_model:`` so empty string is also dropped.
        p = build_dispatch_payload(brief="x", channel_id="c", preferred_model="")
        assert "preferred_model" not in p

    def test_preserves_other_fields(self):
        # Regression guard: per-stage payload must keep the existing fields
        # alongside the new one.
        p = build_dispatch_payload(
            brief="x", channel_id="c", persona="reviewer", wait=False,
            preferred_model="deepseek:deepseek-v4-pro",
        )
        assert p["brief"] == "x"
        assert p["channel_id"] == "c"
        assert p["persona"] == "reviewer"
        assert p["wait_for_result"] is False
        assert p["preferred_model"] == "deepseek:deepseek-v4-pro"
