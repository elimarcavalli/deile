"""Tests for DispatchPayload.timeout_s and max_retries fields (issue #391).

Mirrors test_dispatch_payload_preferred_model.py.
"""

import pytest

from deile.infrastructure.deile_worker_client import (DispatchPayload,
                                                      build_dispatch_payload)


class TestDispatchPayloadFields:
    def _base(self, **kwargs):
        return dict(
            brief="do something",
            channel_id="test-channel",
            **kwargs,
        )

    def test_timeout_s_accepted(self):
        p = DispatchPayload(**self._base(timeout_s=600))
        assert p.timeout_s == 600

    def test_max_retries_accepted(self):
        p = DispatchPayload(**self._base(max_retries=3))
        assert p.max_retries == 3

    def test_max_retries_zero_accepted(self):
        p = DispatchPayload(**self._base(max_retries=0))
        assert p.max_retries == 0

    def test_timeout_s_none_by_default(self):
        p = DispatchPayload(**self._base())
        assert p.timeout_s is None

    def test_max_retries_none_by_default(self):
        p = DispatchPayload(**self._base())
        assert p.max_retries is None

    def test_timeout_s_zero_rejected(self):
        with pytest.raises(Exception):
            DispatchPayload(**self._base(timeout_s=0))

    def test_timeout_s_negative_rejected(self):
        with pytest.raises(Exception):
            DispatchPayload(**self._base(timeout_s=-1))

    def test_max_retries_negative_rejected(self):
        with pytest.raises(Exception):
            DispatchPayload(**self._base(max_retries=-1))

    def test_both_fields_accepted(self):
        p = DispatchPayload(**self._base(timeout_s=900, max_retries=5))
        assert p.timeout_s == 900
        assert p.max_retries == 5

    def test_model_dump_excludes_none(self):
        p = DispatchPayload(**self._base())
        dumped = p.model_dump(exclude_none=True)
        assert "timeout_s" not in dumped
        assert "max_retries" not in dumped

    def test_model_dump_includes_when_set(self):
        p = DispatchPayload(**self._base(timeout_s=600, max_retries=2))
        dumped = p.model_dump(exclude_none=True)
        assert dumped["timeout_s"] == 600
        assert dumped["max_retries"] == 2

    def test_max_retries_zero_included_in_dump(self):
        """0 is falsy but should appear in the wire payload (not excluded)."""
        p = DispatchPayload(**self._base(max_retries=0))
        dumped = p.model_dump(exclude_none=True)
        assert dumped["max_retries"] == 0


class TestBuildDispatchPayload:
    def _call(self, **kwargs):
        return build_dispatch_payload(
            brief="do something",
            channel_id="test-channel",
            **kwargs,
        )

    def test_timeout_s_included(self):
        payload = self._call(timeout_s=600)
        assert payload["timeout_s"] == 600

    def test_max_retries_included(self):
        payload = self._call(max_retries=3)
        assert payload["max_retries"] == 3

    def test_max_retries_zero_included(self):
        payload = self._call(max_retries=0)
        assert payload["max_retries"] == 0

    def test_timeout_s_none_not_in_payload(self):
        payload = self._call(timeout_s=None)
        assert "timeout_s" not in payload

    def test_max_retries_none_not_in_payload(self):
        payload = self._call(max_retries=None)
        assert "max_retries" not in payload

    def test_backward_compat_no_new_fields(self):
        """Callers that don't pass timeout_s/max_retries get same payload."""
        payload = self._call()
        assert "timeout_s" not in payload
        assert "max_retries" not in payload
