"""AC10 — concorrência: 10 threads → value >= 10 — issue #455 D7."""

from __future__ import annotations

import threading

import pytest

from deile.observability import dispatch_metrics as dm
from deile.tests.observability.conftest import dispatch_metric_points

pytestmark = pytest.mark.unit


def test_ten_threads_increment(in_memory_dispatch_metrics_reader):
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        dm.record_dispatch_total(role="worker", outcome="completed")

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    points = dispatch_metric_points(
        in_memory_dispatch_metrics_reader, dm.METRIC_DISPATCH_TOTAL
    )
    # Soma de todos os data points com (role=worker, outcome=completed).
    total = sum(
        v for v, attrs in points if attrs == {"role": "worker", "outcome": "completed"}
    )
    assert total >= 10

    # Apenas 1 init mesmo com 10 threads concorrentes.
    assert dm._init_count == 1
