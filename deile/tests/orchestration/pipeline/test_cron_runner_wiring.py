"""Tests for Gap 1 — CronRunner wired into /pipeline start/stop (issue #164)."""

from __future__ import annotations

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

        monitor_instance = MagicMock()
        monitor_instance.start = AsyncMock()
        cron_runner_instance = MagicMock()
        cron_runner_instance.start = AsyncMock()

        with patch("deile.commands.builtin.pipeline_command.build_default_pipeline_config", return_value=MagicMock()), \
             patch("deile.commands.builtin.pipeline_command.PipelineMonitor", return_value=monitor_instance), \
             patch("deile.cron.store.CronStore", return_value=MagicMock()), \
             patch("deile.cron.agent_bridge.make_fire_callback", return_value=MagicMock()), \
             patch("deile.cron.runner.CronRunner", return_value=cron_runner_instance):
            result = await cmd.execute(ctx)

        cron_runner_instance.start.assert_awaited_once()
        assert agent.cron_runner is cron_runner_instance
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
