"""Regression tests for issue #381 — two cascade bugs.

Bug A (implementer.py): ``wait=True`` was hardcoded in ``_dispatch()``
payload_kwargs, so ``build_dispatch_payload(wait=True)`` was always sent even
when the caller passed ``nowait=True``.  The worker responded synchronously
(blocking 200) instead of fire-and-forget (202), causing the 30-s timeout +
409 CONCURRENT_DISPATCH_BLOCKED dance on every fire-and-forget dispatch.

Bug B (runner.py): ``_start_status_server()`` was called AFTER
``await monitor.start()``.  The catch-up inside ``monitor.start()`` can block
for minutes (N ticks × M sync dispatches) and the readiness probe
(:8768/v1/health) kept failing throughout.  Fix: status server must be
started before ``monitor.start()``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Bug A — wait_for_result propagates nowait correctly
# ──────────────────────────────────────────────────────────────────────────────

class TestBugA_WaitForResultPropagatesnowait:
    """``_dispatch(nowait=True)`` → ``build_dispatch_payload(wait=False)``.
    ``_dispatch(nowait=False)`` → ``build_dispatch_payload(wait=True)``.
    """

    def _make_implementer(self):
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
        from deile.orchestration.pipeline.implementer import WorkerImplementer
        client = MagicMock()
        client.get_resume_info = AsyncMock(return_value=None)
        ledger = MagicMock(spec=DispatchLedger)
        ledger.get = MagicMock(return_value=None)
        impl = WorkerImplementer(
            client=client,
            endpoint_override="http://fake-worker:8766",
            ledger=ledger,
        )
        return impl

    @pytest.mark.parametrize("nowait,expected_wait", [
        (True, False),
        (False, True),
    ])
    async def test_wait_flag_matches_not_nowait(self, nowait, expected_wait):
        """build_dispatch_payload receives wait=not(nowait)."""
        impl = self._make_implementer()

        captured: dict = {}

        def fake_build_payload(**kwargs):
            captured.update(kwargs)
            return {"brief": kwargs.get("brief", ""), "wait_for_result": kwargs["wait"]}

        fake_response = (
            {"task_id": "t1"}
            if nowait
            else {"result": "ok", "text": "done"}
        )

        with patch(
            "deile.infrastructure.deile_worker_client.build_dispatch_payload",
            side_effect=fake_build_payload,
        ), patch.object(
            impl, "_post_dispatch", new=AsyncMock(return_value=fake_response),
        ):
            await impl._dispatch(
                "brief text",
                channel_id="pipeline-issue-42",
                persona="developer",
                stage="implement",
                nowait=nowait,
            )

        assert "wait" in captured, "build_dispatch_payload was not called"
        assert captured["wait"] is expected_wait, (
            f"nowait={nowait} → expected build_dispatch_payload(wait={expected_wait}), "
            f"got wait={captured['wait']!r} — Bug A regression"
        )

    async def test_nowait_true_sends_wait_false(self):
        """Explicit regression: fire-and-forget dispatch must send wait=False."""
        impl = self._make_implementer()
        captured: dict = {}

        def fake_build_payload(**kwargs):
            captured.update(kwargs)
            return {"wait_for_result": kwargs["wait"]}

        with patch(
            "deile.infrastructure.deile_worker_client.build_dispatch_payload",
            side_effect=fake_build_payload,
        ), patch.object(
            impl, "_post_dispatch", new=AsyncMock(return_value={"task_id": "abc"}),
        ):
            outcome = await impl._dispatch(
                "a brief",
                channel_id="pipeline-issue-1",
                persona="developer",
                nowait=True,
            )

        assert captured.get("wait") is False, (
            "Bug A: hardcoded wait=True in payload_kwargs — "
            f"build_dispatch_payload received wait={captured.get('wait')!r}"
        )
        assert outcome.ok is True


# ──────────────────────────────────────────────────────────────────────────────
# Bug B — status server starts before monitor.start()
# ──────────────────────────────────────────────────────────────────────────────

class TestBugB_StatusServerBeforeMonitorStart:
    """``run_pipeline_forever()`` must call ``_start_status_server`` before
    ``monitor.start()`` so the readiness probe can respond during catch-up."""

    async def test_status_server_starts_before_monitor(self):
        call_order: list[str] = []

        async def fake_start_status(monitor):
            call_order.append("status_server")
            return None  # server-less mode

        mock_monitor = MagicMock()
        mock_monitor.identity.monitor_id = "test-id"
        mock_monitor.config.repo = "owner/repo"
        mock_monitor.config.dispatch_mode = "worker"
        mock_monitor.config.poll_interval_seconds = 60

        async def fake_monitor_start():
            call_order.append("monitor_start")

        mock_monitor.start = AsyncMock(side_effect=fake_monitor_start)
        mock_monitor.stop = AsyncMock()

        pre_set = asyncio.Event()
        pre_set.set()

        with patch(
            "deile.orchestration.pipeline.monitor.build_default_pipeline_config",
            return_value=MagicMock(),
        ), patch(
            "deile.orchestration.pipeline.monitor.PipelineMonitor",
            return_value=mock_monitor,
        ), patch(
            "deile.orchestration.pipeline.runner._build_notifier",
            return_value=MagicMock(),
        ), patch(
            "deile.orchestration.pipeline.runner._start_status_server",
            side_effect=fake_start_status,
        ), patch(
            "asyncio.Event",
            return_value=pre_set,
        ):
            from deile.orchestration.pipeline import runner
            await runner.run_pipeline_forever()

        assert call_order == ["status_server", "monitor_start"], (
            f"Bug B regression: expected status_server before monitor_start, "
            f"got {call_order}"
        )
