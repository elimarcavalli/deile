"""``/pipeline`` command — start/stop/status of the autonomous pipeline.

Usage:
    /pipeline start [--identity <id>] [--schedule-file <path>] [--no-pid-lock]
                        start the 1-min polling loop
    /pipeline stop          stop the polling loop
    /pipeline status        print stats + current state
    /pipeline tick          run a single tick synchronously (debug)
    /pipeline reset <issue#>    remove ~batch: + ~by:* labels from an issue

Flags (all optional, override env vars):
    --identity <id>        DEILE_PIPELINE_MONITOR_ID override
    --schedule-file <path> path to a custom schedule YAML
    --no-pid-lock          disable PID lockfile (useful for dev/test)

The command is idempotent: running ``start`` twice does nothing on the second
call. The monitor instance is held on ``context.agent.pipeline_monitor``.
"""

from __future__ import annotations

import argparse
import logging
import shlex
from pathlib import Path
from typing import Optional

from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.config.manager import CommandConfig
from deile.orchestration.pipeline.constants import PIPELINE_DEFAULT_REPO
from deile.orchestration.pipeline.labels import BATCH_LABEL_PREFIX
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)

logger = logging.getLogger(__name__)


def _parse_start_flags(raw: str):
    """Parse optional flags from the ``start`` subcommand tail.

    Returns a namespace with ``identity``, ``schedule_file``, ``no_pid_lock``.
    Unknown tokens are silently ignored so future flags don't break old callers.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--identity", default=None)
    parser.add_argument("--schedule-file", dest="schedule_file", default=None)
    parser.add_argument("--no-pid-lock", dest="no_pid_lock", action="store_true", default=False)
    try:
        ns, _ = parser.parse_known_args(shlex.split(raw))
    except (ValueError, SystemExit):
        ns = argparse.Namespace(identity=None, schedule_file=None, no_pid_lock=False)
    return ns


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

    # Pipeline exposes 3 CLI flags (one per sub-command), all via cli_extra_flags.
    cli_flag = None
    cli_requires_provider = False
    cli_extra_flags = {
        "--pipeline-status": {
            "subcommand": "status",
            "help": "Show autonomous pipeline status and exit.",
            "takes_arg": False,
            "requires_provider": False,
        },
        "--pipeline-start": {
            "subcommand": "start",
            "help": "Start the autonomous pipeline polling loop and exit.",
            "takes_arg": False,
            "requires_provider": False,
        },
        "--pipeline-stop": {
            "subcommand": "stop",
            "help": "Stop the autonomous pipeline polling loop and exit.",
            "takes_arg": False,
            "requires_provider": False,
        },
    }

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
        parts = context.args.strip().split(None, 1)
        sub = parts[0].lower() if parts else "status"
        tail = parts[1] if len(parts) > 1 else ""
        agent = context.agent
        monitor: Optional[PipelineMonitor] = getattr(agent, "pipeline_monitor", None)

        if monitor is None:
            from deile.config.settings import get_settings
            from deile.orchestration.pipeline.post_merge_callback import \
                make_post_merge_callback
            from deile.orchestration.pipeline.review_callback import \
                make_review_callback

            cfg = PipelineConfig(
                repo=_resolve_repo(),
                base_repo_path=_resolve_base_path(),
                notify_user_id=get_settings().pipeline_notify_user_id,
            )
            monitor = PipelineMonitor(
                cfg,
                review_callback=make_review_callback(agent),
                post_merge_callback=make_post_merge_callback(agent),
            )
            # Persist on agent so subsequent invocations reuse the instance.
            # When invoked from CLI one-shot mode (#126) agent may be None —
            # the monitor is single-use in that case, no caching needed.
            if agent is not None:
                try:
                    agent.pipeline_monitor = monitor  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    # MagicMock/SimpleNamespace without slots; non-fatal.
                    pass

        if sub == "start":
            flags = _parse_start_flags(tail)
            # Flags override the monitor's current config when supplied.
            if flags.identity or flags.schedule_file or flags.no_pid_lock:
                from deile.config.settings import get_settings
                from deile.orchestration.pipeline.identity import \
                    MonitorIdentity
                from deile.orchestration.pipeline.post_merge_callback import \
                    make_post_merge_callback
                from deile.orchestration.pipeline.review_callback import \
                    make_review_callback
                from deile.orchestration.pipeline.scheduler import \
                    ScheduleStore

                cfg = PipelineConfig(
                    repo=_resolve_repo(),
                    base_repo_path=_resolve_base_path(),
                    notify_user_id=get_settings().pipeline_notify_user_id,
                    use_pid_lock=not flags.no_pid_lock,
                )
                identity = (
                    MonitorIdentity(monitor_id=flags.identity)
                    if flags.identity
                    else None
                )
                schedule_store = (
                    ScheduleStore(
                        Path(flags.schedule_file).parent,
                        monitor_id=Path(flags.schedule_file).stem,
                    )
                    if flags.schedule_file
                    else None
                )
                monitor = PipelineMonitor(
                    cfg,
                    identity=identity,
                    schedule_store=schedule_store,
                    review_callback=make_review_callback(agent),
                    post_merge_callback=make_post_merge_callback(agent),
                )
                agent.pipeline_monitor = monitor  # type: ignore[attr-defined]
            await monitor.start()

            # Wire CronRunner so scheduled tasks fire alongside the pipeline.
            from deile.cron.agent_bridge import \
                make_fire_callback as _make_cron_cb
            from deile.cron.runner import CronRunner  # noqa: PLC0415
            from deile.cron.store import CronStore, resolve_db_path

            _cron_store = CronStore(resolve_db_path())

            async def _cron_agent_provider():
                return agent

            cron_runner = CronRunner(
                _cron_store,
                fire_callback=_make_cron_cb(_cron_agent_provider),
            )
            await cron_runner.start()
            if agent is not None:
                try:
                    agent.cron_runner = cron_runner  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
            logger.info("cron runner started (db=%s)", _cron_store.db_path)

            return CommandResult(
                success=True,
                content=(
                    f"✅ pipeline iniciado (repo={monitor.config.repo}, "
                    f"interval={monitor.config.poll_interval_seconds}s, "
                    f"identity={monitor.identity.monitor_id}) | "
                    f"cron iniciado (db={_cron_store.db_path})"
                ),
            )
        if sub == "stop":
            _cron_runner = getattr(agent, "cron_runner", None) if agent is not None else None
            if _cron_runner is not None and _cron_runner.is_running:
                await _cron_runner.stop()
                logger.info("cron runner stopped")
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
            # Remove ~batch: + ~by:* labels from an issue so it can be re-processed.
            if not tail.strip():
                return CommandResult(
                    success=False, content="❌ uso: /pipeline reset <issue_number>"
                )
            raw_num = tail.strip().split()[0].lstrip("#")
            try:
                issue_number = int(raw_num)
            except ValueError:
                return CommandResult(
                    success=False, content=f"❌ número de issue inválido: {raw_num!r}"
                )
            return await _reset_issue(monitor, issue_number)

        # default: status
        s = monitor.stats
        running = monitor.is_running
        cron_runner = getattr(agent, "cron_runner", None) if agent is not None else None
        cron_state = ""
        if cron_runner is not None:
            state = "rodando" if cron_runner.is_running else "parado"
            cron_state = f"\n  cron={state} fired={cron_runner.fired_count}"
        return CommandResult(
            success=True,
            content=(
                f"📊 pipeline {'rodando' if running else 'parado'} | repo={monitor.config.repo}\n"
                f"  ticks={s.ticks}  reviewed={s.issues_reviewed}  "
                f"implemented={s.issues_implemented}  prs={s.prs_reviewed}  "
                f"errors={s.errors}  gh_errors={s.gh_errors}  claude_errors={s.claude_errors}"
                f"{cron_state}"
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
