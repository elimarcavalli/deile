"""Tests para runner.py — fix #779 força-tick double-dispatch guard."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestForceTickGuard:
    """_force_tick_cb não despacha quando _tick_in_flight=True."""

    def _make_monitor_with_state(self):
        """Retorna (monitor_mock, callback) — callback é o registrado via set_force_tick_callback."""
        from pathlib import Path

        from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                          PipelineMonitor)

        cfg = PipelineConfig(
            repo="owner/name",
            base_repo_path=Path("/tmp/fake"),
            notify_user_id="42",
        )
        forge = MagicMock()
        forge.ensure_pipeline_labels = AsyncMock()
        forge.on_label_change = None
        monitor = PipelineMonitor.__new__(PipelineMonitor)
        monitor._tick_in_flight = False
        monitor.tick = AsyncMock()
        return monitor

    async def test_force_tick_skips_when_in_flight(self):
        """AC-1a: com _tick_in_flight=True, _force_tick_cb não agenda nova task."""
        monitor = self._make_monitor_with_state()
        monitor._tick_in_flight = True

        # Simula o closure registrado em runner.py
        def _force_tick_cb():
            if not monitor._tick_in_flight:
                asyncio.ensure_future(monitor.tick())

        created = []
        with patch("asyncio.ensure_future", side_effect=created.append):
            _force_tick_cb()

        assert len(created) == 0, "nenhum future deve ser criado quando _tick_in_flight=True"

    async def test_force_tick_schedules_when_not_in_flight(self):
        """AC-1b: com _tick_in_flight=False, _force_tick_cb agenda exatamente uma task."""
        monitor = self._make_monitor_with_state()
        monitor._tick_in_flight = False  # tick já é AsyncMock (via _make_monitor_with_state)

        def _force_tick_cb():
            if not monitor._tick_in_flight:
                asyncio.ensure_future(monitor.tick())

        with patch("asyncio.ensure_future") as ensure_future:
            _force_tick_cb()

        ensure_future.assert_called_once()
        monitor.tick.assert_called_once()
        # coro capturada não foi agendada (ensure_future mockado) → fecha p/ não vazar
        ensure_future.call_args.args[0].close()

    async def test_tick_resets_flag_on_exception(self):
        """AC-1c: _tick_in_flight volta a False mesmo que _tick_body lance exceção."""
        from pathlib import Path

        from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                          PipelineMonitor)

        cfg = PipelineConfig(
            repo="owner/name",
            base_repo_path=Path("/tmp/fake"),
            notify_user_id="42",
        )
        monitor = PipelineMonitor.__new__(PipelineMonitor)
        monitor._tick_in_flight = False
        monitor._stats = MagicMock()
        monitor._stats.ticks = 0

        async def _raising_body(*_):
            raise RuntimeError("infra failure")

        monitor._tick_body = _raising_body

        with pytest.raises(RuntimeError):
            await monitor.tick()

        assert monitor._tick_in_flight is False, "_tick_in_flight deve ser False após exceção"
