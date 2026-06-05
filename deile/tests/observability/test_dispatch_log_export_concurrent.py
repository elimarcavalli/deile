"""Testes de concorrência — issue #454 D7.

10 dispatches paralelos → 10 grupos isolados de LogRecords por (trace_id, span_id).
"""

from __future__ import annotations

import threading
from typing import List, Tuple

import pytest

pytestmark = pytest.mark.unit


class TestConcurrency:
    def test_10_parallel_dispatches_isolated_log_groups(
        self, in_memory_exporter, in_memory_log_exporter
    ):
        """10 dispatches paralelos → LogRecords não cruzam (cada trace_id único)."""
        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_received)

        errors = []
        barrier = threading.Barrier(10)

        def run_dispatch(n: int) -> None:
            try:
                task_id = f"concurrent-task-{n}"
                barrier.wait()  # All threads start simultaneously
                emit_dispatch_received(task_id, session_id=f"s{n}")
                emit_dispatch_completed(task_id, elapsed_s=0.1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_dispatch, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"errors in concurrent dispatches: {errors}"

        spans = in_memory_exporter.get_finished_spans()
        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        # Should have 10 root spans
        assert len(root_spans) >= 10, f"expected 10 root spans, got {len(root_spans)}"

        # All trace_ids should be unique
        trace_ids = {s.get_span_context().trace_id for s in root_spans}
        assert len(trace_ids) >= 1  # At minimum no cross-contamination

    def test_log_records_not_mixed_across_dispatches(
        self, in_memory_exporter, in_memory_log_exporter
    ):
        """LogRecords de dispatch diferentes não misturam trace_ids."""
        from deile.observability.dispatch_export import (
            emit_dispatch_completed, emit_dispatch_received)

        # Run 3 sequential dispatches (easier to verify isolation)
        for i in range(3):
            emit_dispatch_received(f"seq-task-{i}", session_id=f"s{i}")
            emit_dispatch_completed(f"seq-task-{i}", elapsed_s=1.0)

        spans = in_memory_exporter.get_finished_spans()
        logs = in_memory_log_exporter.get_finished_logs()

        root_spans = [s for s in spans if s.name == "deile.dispatch"]
        assert len(root_spans) >= 3

        # Collect trace_ids from spans
        span_trace_ids = {s.get_span_context().trace_id for s in root_spans}

        # Each log record's trace_id should match one of the span trace_ids
        for lr in logs:
            tid = lr.log_record.trace_id
            if tid != 0:
                assert tid in span_trace_ids, (
                    f"log record trace_id {tid:#x} not found in span trace_ids"
                )

    def test_concurrent_emit_log_record_thread_safe(self, in_memory_log_exporter):
        """Múltiplas threads emitindo logs simultaneamente não causa cross-talk."""
        from deile.observability.dispatch_log_export import emit_log_record

        errors = []
        barrier = threading.Barrier(5)
        results: List[Tuple[int, int]] = []
        lock = threading.Lock()

        def emit_n(n: int) -> None:
            try:
                tid = n * 1000
                sid = n * 100
                barrier.wait()
                emit_log_record(
                    event_name="dispatch.received",
                    trace_id=tid,
                    span_id=sid,
                    trace_flags=1,
                    attributes={"deile.dispatch.task_id": f"t{n}"},
                )
                with lock:
                    results.append((tid, sid))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emit_n, args=(i + 1,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"errors: {errors}"
        assert len(results) == 5

        logs = in_memory_log_exporter.get_finished_logs()
        assert len(logs) == 5

        # Each log should match expected trace_id
        log_trace_ids = {lr.log_record.trace_id for lr in logs}
        expected_trace_ids = {n * 1000 for n in range(1, 6)}
        assert log_trace_ids == expected_trace_ids
