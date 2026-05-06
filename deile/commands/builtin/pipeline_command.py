"""``/pipeline`` command — start/stop/status of the autonomous pipeline.

Usage:
    /pipeline start         start the 1-min polling loop
    /pipeline stop          stop the polling loop
    /pipeline status        print stats + current state
    /pipeline tick          run a single tick synchronously (debug)

The command is idempotent: running ``start`` twice does nothing on the second
call. The monitor instance is held on ``context.agent.pipeline_monitor``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.config.manager import CommandConfig
from deile.orchestration.pipeline.constants import PIPELINE_DEFAULT_REPO
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)


def _resolve_repo() -> str:
    return os.environ.get("DEILE_PIPELINE_REPO", PIPELINE_DEFAULT_REPO)


def _resolve_base_path() -> Path:
    """Find the DEILE repo root from the current working directory."""
    raw = os.environ.get("DEILE_PIPELINE_BASE_PATH")
    if raw:
        return Path(raw).resolve()
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").is_dir() and (ancestor / "deile.py").is_file():
            return ancestor
    return cwd


class PipelineCommand(DirectCommand):
    """``/pipeline {start|stop|status|tick}``."""

    def __init__(self) -> None:
        super().__init__(
            CommandConfig(
                name="pipeline",
                description="Controla o pipeline autônomo de issues/PRs (start|stop|status|tick)",
                action="pipeline",
            )
        )
        self.category = "orchestration"

    async def execute(self, context: CommandContext) -> CommandResult:
        parts = context.args.strip().split(None, 1)
        sub = parts[0].lower() if parts else "status"
        agent = context.agent
        monitor: Optional[PipelineMonitor] = getattr(agent, "pipeline_monitor", None)

        if monitor is None:
            cfg = PipelineConfig(
                repo=_resolve_repo(),
                base_repo_path=_resolve_base_path(),
                notify_user_id=os.environ.get("DEILE_PIPELINE_NOTIFY_USER_ID"),
            )
            monitor = PipelineMonitor(cfg)
            agent.pipeline_monitor = monitor  # type: ignore[attr-defined]

        if sub == "start":
            await monitor.start()
            return CommandResult(
                success=True,
                content=(
                    f"✅ pipeline iniciado (repo={monitor.config.repo}, "
                    f"interval={monitor.config.poll_interval_seconds}s)"
                ),
            )
        if sub == "stop":
            await monitor.stop()
            return CommandResult(success=True, content="🛑 pipeline parado")
        if sub == "tick":
            await monitor.tick()
            s = monitor.stats
            return CommandResult(
                success=True,
                content=(
                    f"🔄 single tick OK — ticks={s.ticks} "
                    f"reviewed={s.issues_reviewed} implemented={s.issues_implemented} "
                    f"prs={s.prs_reviewed} errors={s.errors}"
                ),
            )
        # default: status
        s = monitor.stats
        running = monitor.is_running
        return CommandResult(
            success=True,
            content=(
                f"📊 pipeline {'rodando' if running else 'parado'} | repo={monitor.config.repo}\n"
                f"  ticks={s.ticks}  reviewed={s.issues_reviewed}  "
                f"implemented={s.issues_implemented}  prs={s.prs_reviewed}  errors={s.errors}"
            ),
        )
