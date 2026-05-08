"""``/pipeline`` command — start/stop/status of the autonomous pipeline.

Usage:
    /pipeline start             start the 1-min polling loop
    /pipeline stop              stop the polling loop
    /pipeline status            print stats + current state
    /pipeline tick              run a single tick synchronously (debug)
    /pipeline reset <issue#>    remove ~batch: + ~by:* labels from an issue (gap #34)

The command is idempotent: running ``start`` twice does nothing on the second
call. The monitor instance is held on ``context.agent.pipeline_monitor``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.config.manager import CommandConfig
from deile.orchestration.pipeline.constants import PIPELINE_DEFAULT_REPO
from deile.orchestration.pipeline.labels import BATCH_LABEL_PREFIX
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)

logger = logging.getLogger(__name__)


def _resolve_repo() -> str:
    from deile.config.settings import get_settings

    return get_settings().pipeline_repo or PIPELINE_DEFAULT_REPO


def _resolve_base_path() -> Path:
    """Find the DEILE repo root from settings or CWD ancestor search."""
    from deile.config.settings import get_settings

    s = get_settings()
    if s.pipeline_base_path:
        return s.pipeline_base_path.resolve()
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").is_dir() and (ancestor / "deile.py").is_file():
            return ancestor
    return cwd


class PipelineCommand(DirectCommand):
    """``/pipeline {start|stop|status|tick|reset}``."""

    def __init__(self) -> None:
        super().__init__(
            CommandConfig(
                name="pipeline",
                description=(
                    "Controla o pipeline autônomo de issues/PRs "
                    "(start|stop|status|tick|reset <issue#>)"
                ),
                action="pipeline",
            )
        )
        self.category = "orchestration"

    async def execute(self, context: CommandContext) -> CommandResult:
        parts = context.args.strip().split(None, 2)
        sub = parts[0].lower() if parts else "status"
        agent = context.agent
        monitor: Optional[PipelineMonitor] = getattr(agent, "pipeline_monitor", None)

        if monitor is None:
            from deile.config.settings import get_settings
            from deile.orchestration.pipeline.review_callback import \
                make_review_callback

            cfg = PipelineConfig(
                repo=_resolve_repo(),
                base_repo_path=_resolve_base_path(),
                notify_user_id=get_settings().pipeline_notify_user_id,
            )
            monitor = PipelineMonitor(cfg, review_callback=make_review_callback(agent))
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
        if sub == "reset":
            # gap #34: remove ~batch: + ~by:* labels from an issue so it can be re-processed
            if len(parts) < 2:
                return CommandResult(
                    success=False, content="❌ uso: /pipeline reset <issue_number>"
                )
            try:
                issue_number = int(parts[1].lstrip("#"))
            except ValueError:
                return CommandResult(
                    success=False, content=f"❌ número de issue inválido: {parts[1]!r}"
                )
            return await _reset_issue(monitor, issue_number)

        # default: status
        s = monitor.stats
        running = monitor.is_running
        return CommandResult(
            success=True,
            content=(
                f"📊 pipeline {'rodando' if running else 'parado'} | repo={monitor.config.repo}\n"
                f"  ticks={s.ticks}  reviewed={s.issues_reviewed}  "
                f"implemented={s.issues_implemented}  prs={s.prs_reviewed}  "
                f"errors={s.errors}  gh_errors={s.gh_errors}  claude_errors={s.claude_errors}"
            ),
        )


async def _reset_issue(monitor: PipelineMonitor, issue_number: int) -> CommandResult:
    """Remove pipeline lock labels from *issue_number* (gap #34)."""
    from deile.orchestration.pipeline.github_client import GhCommandError

    github = monitor.github
    try:
        issue = await github.get_issue(issue_number)
    except GhCommandError as exc:
        return CommandResult(success=False, content=f"❌ gh error: {exc}")

    to_remove = [
        lb for lb in issue.labels
        if lb.startswith(BATCH_LABEL_PREFIX) or lb.startswith("~by:")
    ]
    if not to_remove:
        return CommandResult(
            success=True,
            content=f"ℹ️ issue #{issue_number} não tem labels de lock para remover.",
        )

    try:
        await github.remove_labels("issue", issue_number, to_remove)
    except GhCommandError as exc:
        return CommandResult(success=False, content=f"❌ falha ao remover labels: {exc}")

    logger.info(
        "pipeline reset: removed labels %s from issue #%d", to_remove, issue_number
    )
    return CommandResult(
        success=True,
        content=(
            f"✅ issue #{issue_number} desbloqueada — labels removidas: "
            f"{', '.join(to_remove)}.\n"
            f"A issue será reprocessada no próximo tick."
        ),
    )
