"""Tests for Gap 1 — CronRunner wired into /pipeline start/stop (issue #164)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from deile.commands.base import CommandContext
from deile.commands.builtin.pipeline_command import PipelineCommand


def _make_context(args: str = "start") -> tuple[CommandContext, MagicMock]:
    agent = MagicMock()
    agent.pipeline_monitor = None
    agent.cron_runner = None
    ctx = CommandContext(agent=agent, args=args, user_input=f"/{args}")
    return ctx, agent


class TestCronRunnerStart:
    async def test_start_creates_and_starts_cron_runner(self):
        ctx, agent = _make_context("start")
        cmd = PipelineCommand()

        cron_store_mock = MagicMock()
        cron_store_mock.db_path = Path("/tmp/cron.db")
        with patch("deile.commands.builtin.pipeline_command._resolve_repo", return_value="o/r"), \
             patch("deile.commands.builtin.pipeline_command._resolve_base_path", return_value=Path("/tmp/x")), \
             patch("deile.commands.builtin.pipeline_command.PipelineMonitor") as MockMonitor, \
             patch("deile.orchestration.pipeline.review_callback.make_review_callback", return_value=AsyncMock()), \
             patch("deile.cron.store.CronStore", return_value=cron_store_mock), \
             patch("deile.cron.store.resolve_db_path", return_value=Path("/tmp/cron.db")), \
             patch("deile.cron.runner.CronRunner.start", new_callable=AsyncMock), \
             patch("deile.cron.runner.CronRunner.is_running", new_callable=lambda: property(lambda self: True)):
            monitor_instance = MagicMock()
            monitor_instance.start = AsyncMock()
            monitor_instance.config.repo = "o/r"
            monitor_instance.config.poll_interval_seconds = 60
            monitor_instance.identity.monitor_id = "default"
            MockMonitor.return_value = monitor_instance
            result = await cmd.execute(ctx)

        assert result.success
        assert "cron" in result.content.lower()

    async def test_stop_stops_cron_runner(self):
        ctx, agent = _make_context("stop")
        cron_runner = MagicMock()
        cron_runner.is_running = True
        cron_runner.stop = AsyncMock()
        agent.cron_runner = cron_runner

        monitor_mock = MagicMock()
        monitor_mock.is_running = True
        monitor_mock.stop = AsyncMock()
        agent.pipeline_monitor = monitor_mock

        cmd = PipelineCommand()
        result = await cmd.execute(ctx)

        cron_runner.stop.assert_awaited_once()
        assert result.success

    async def test_stop_noop_when_no_cron_runner(self):
        ctx, agent = _make_context("stop")
        agent.cron_runner = None
        monitor_mock = MagicMock()
        monitor_mock.is_running = True
        monitor_mock.stop = AsyncMock()
        agent.pipeline_monitor = monitor_mock

        cmd = PipelineCommand()
        result = await cmd.execute(ctx)
        assert result.success

    async def test_status_shows_cron_info(self):
        ctx, agent = _make_context("status")
        cron_runner = MagicMock()
        cron_runner.is_running = True
        cron_runner.fired_count = 3
        agent.cron_runner = cron_runner

        monitor_mock = MagicMock()
        monitor_mock.is_running = True
        monitor_mock.stats.ticks = 5
        monitor_mock.stats.issues_reviewed = 0
        monitor_mock.stats.issues_implemented = 0
        monitor_mock.stats.prs_reviewed = 0
        monitor_mock.stats.errors = 0
        monitor_mock.stats.gh_errors = 0
        monitor_mock.stats.claude_errors = 0
        monitor_mock.config.repo = "o/r"
        agent.pipeline_monitor = monitor_mock

        cmd = PipelineCommand()
        result = await cmd.execute(ctx)

        assert result.success
        assert "cron" in result.content.lower()
        assert "3" in result.content  # fired_count
