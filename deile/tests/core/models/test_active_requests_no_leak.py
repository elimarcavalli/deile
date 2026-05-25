"""Regression test for the active_requests unbounded counter bug.

Before the fix, ``ModelMetrics.record_request`` incremented
``active_requests`` on every selection but nothing decremented it. Over
a long-running session the counter grew without bound, corrupting
``LEAST_BUSY`` and ``LOAD_BALANCED`` strategies (which compared
providers by this counter).

After the fix, ``active_requests`` is documented as deprecated and the
two strategies key on ``total_requests`` instead (a monotonic-correct
proxy for historical load).
"""

from __future__ import annotations

from deile.core.models.routing_strategies import ModelMetrics


def test_record_request_does_not_increment_active_requests() -> None:
    """The broken increment must be gone — active_requests must NOT grow."""
    m = ModelMetrics()
    for _ in range(100):
        m.record_request()
    assert m.total_requests == 100
    # active_requests was never decremented elsewhere — keep it at 0 so
    # LEAST_BUSY/LOAD_BALANCED stay correct over long runs.
    assert m.active_requests == 0
    assert m.last_used > 0


def test_metrics_active_requests_default_zero() -> None:
    """Default value must be 0 for compatibility with stats consumers."""
    m = ModelMetrics()
    assert m.active_requests == 0
