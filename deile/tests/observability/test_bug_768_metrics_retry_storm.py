"""xfail test for bug #768: OtlpMetrics retry storm on _build_provider failure.

Bug: When _build_provider() raises, the except block sets self._meter = None
(same value as before the failure). The guard `if self._meter is not None`
never triggers. Every subsequent call to _ensure_meter() (one per metric
emission) retries _build_provider(), causing a storm of SDK initialization
attempts.

Fix: Set self._meter = _SETUP_FAILED sentinel on failure; short-circuit in guard.
Tracker: #768
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 metrics-retry-storm — fix pending tracker #768",
)
def test_ensure_meter_does_not_retry_after_build_failure() -> None:
    """_ensure_meter() must call _build_provider() exactly once on repeated failure.

    When the bug is present:
      - _build_provider() is called N times for N calls to _ensure_meter()
      - assertion call_count == 1 fails -> xfail

    When fixed:
      - _build_provider() is called exactly once; sentinel short-circuits retries
      - assertion passes -> xpass
    """
    try:
        from deile.observability.metrics import OtlpMetrics  # noqa: PLC0415
        from deile.observability import get_observability_config  # noqa: PLC0415
    except ImportError:
        pytest.skip("OtlpMetrics not available in this environment")

    config = get_observability_config()
    m = OtlpMetrics(config)

    call_count = [0]

    def bad_build(self):  # noqa: ANN001
        call_count[0] += 1
        raise RuntimeError("simulated OTLP init failure")

    with patch.object(OtlpMetrics, "_build_provider", bad_build):
        for _ in range(5):
            m._ensure_meter()

    assert call_count[0] == 1, (
        f"Expected _build_provider() called exactly once after failure, "
        f"got {call_count[0]} calls. Retry storm confirmed — bug still present."
    )
