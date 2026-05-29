"""Regression tests for issue #381.

Bug A — ``wait_for_result`` hardcoded ``True`` in
``WorkerImplementer._dispatch()``: the payload must reflect ``not nowait``
so fire-and-forget dispatches actually send ``wait_for_result=False`` to the
worker, preventing the spurious 409 / WORKER_TIMEOUT cycle.

Bug B — ``_start_status_server()`` called AFTER ``await monitor.start()``:
the K8s readiness probe at ``:8768/v1/health`` must be alive before catch-up
starts, not after.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── shared test helpers ─────────────────────────────────────────────────────

def _make_monitor():
    monitor = MagicMock()
    monitor.config = SimpleNamespace(
        repo="owner/name", main_branch="main", base_repo_path=Path("/tmp/fake"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    monitor.forge.config = MagicMock()
    return monitor


def _issue(number: int = 1):
    return SimpleNamespace(number=number, title="t", body="b")


def _pr(number: int = 7):
    return SimpleNamespace(
        number=number, title="t", head_ref="auto/issue-7",
        url=f"https://github.com/owner/name/pull/{number}",
    )


class _RecordingClient:
    """Captures the dispatch payload and wait kwarg; returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.last_payload: dict | None = None
        self.last_wait: bool | None = None

    async def dispatch(self, payload, *, wait):
        self.last_payload = payload
        self.last_wait = wait
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ─── Bug A: wait_for_result in payload ───────────────────────────────────────

class TestBugAWaitForResult:
    """``_dispatch(nowait=True)`` must set ``wait_for_result=False`` in the
    payload sent to the worker.  Before the fix (PR #374), the kwarg was
    hardcoded to ``wait=True``, so fire-and-forget dispatches still asked the
    worker to respond synchronously."""

    async def test_implement_fresh_payload_has_wait_for_result_false(self):
        """A fresh implement dispatch is fire-and-forget (``nowait=True``).

        Both the transport-level ``wait`` kwarg AND the payload field
        ``wait_for_result`` must be ``False``.  The regression was that only
        the transport kwarg respected ``nowait`` while the payload was stuck
        at ``True``.
        """
        from deile.orchestration.pipeline.implementer import WorkerImplementer

        client = _RecordingClient({"task_id": "abc", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.implement(_make_monitor(), _issue())

        assert out.ok is True
        assert client.last_payload is not None
        assert client.last_payload["wait_for_result"] is False, (
            "Bug A regression: implement() must send wait_for_result=False "
            "for fire-and-forget dispatch (nowait=True)"
        )
        assert client.last_wait is False, (
            "Transport wait kwarg must also be False for fire-and-forget"
        )

    async def test_review_payload_has_wait_for_result_true(self):
        """Review dispatch is synchronous (``nowait=False`` default).

        ``wait_for_result`` must be ``True`` so the worker blocks and returns
        the structured result the stage handler needs.
        """
        from deile.orchestration.pipeline.implementer import WorkerImplementer

        client = _RecordingClient(
            {"ok": True, "summary": "https://github.com/owner/name/pull/7 MERGED"}
        )
        impl = WorkerImplementer(client=client)
        out = await impl.review(_make_monitor(), _pr())

        assert out.ok is True
        assert client.last_payload is not None
        assert client.last_payload["wait_for_result"] is True, (
            "Review dispatch must be synchronous (wait_for_result=True)"
        )
        assert client.last_wait is True


# ─── Bug B: status server startup order ─────────────────────────────────────

class TestBugBStartupOrder:
    """``_start_status_server()`` must be awaited BEFORE ``monitor.start()``
    in ``run_pipeline_forever()``.

    Before the fix, the server was started *after* ``monitor.start()``.  The
    catch-up inside ``monitor.start()`` can block for minutes (N ticks × M
    synchronous dispatches), keeping port 8768 closed the whole time and
    causing continuous readiness-probe failures (``0/1 Running``).
    """

    async def test_status_server_starts_before_monitor(self, monkeypatch):
        """``_start_status_server`` must be called before ``monitor.start()``."""
        from deile.orchestration.pipeline.runner import run_pipeline_forever

        call_order: list[str] = []

        class _ImmediateEvent:
            """asyncio.Event replacement: ``wait()`` returns immediately so the
            run loop terminates without blocking the test."""

            def set(self) -> None:
                pass

            async def wait(self) -> None:
                pass

        async def fake_start_server(monitor):
            call_order.append("status_server")
            return None

        monitor_mock = MagicMock()
        monitor_mock.identity.monitor_id = "test-id"
        monitor_mock.start = AsyncMock(
            side_effect=lambda: call_order.append("monitor_start")
        )
        monitor_mock.stop = AsyncMock()

        cfg_mock = MagicMock()
        cfg_mock.repo = "x/y"
        cfg_mock.dispatch_mode = "deile_worker"
        cfg_mock.poll_interval_seconds = 60
        cfg_mock.notify_user_id = None

        # Swap asyncio.Event so stop.wait() returns immediately.
        monkeypatch.setattr(asyncio, "Event", _ImmediateEvent)

        with (
            patch(
                "deile.orchestration.pipeline.runner._start_status_server",
                fake_start_server,
            ),
            patch(
                "deile.orchestration.pipeline.runner._build_notifier",
                return_value=MagicMock(),
            ),
            patch(
                "deile.orchestration.pipeline.monitor.build_default_pipeline_config",
                return_value=cfg_mock,
            ),
            patch(
                "deile.orchestration.pipeline.monitor.PipelineMonitor",
                return_value=monitor_mock,
            ),
        ):
            await run_pipeline_forever()

        assert "status_server" in call_order, "status server was never started"
        assert "monitor_start" in call_order, "monitor.start() was never called"
        assert call_order.index("status_server") < call_order.index("monitor_start"), (
            f"Bug B regression: status server must start BEFORE monitor.start() "
            f"but got call order {call_order}"
        )
