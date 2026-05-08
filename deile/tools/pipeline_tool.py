"""PipelineTool — LLM-callable interface to the autonomous pipeline.

Sibling to the ``/pipeline`` slash command (``deile/commands/builtin/pipeline_command.py``)
but registered as a Tool so the LLM can invoke it from natural language
(e.g. user via Discord: "verifica o status do pipeline pra mim" → DEILE
chooses to call this tool with action='status').

Both surfaces share the same :class:`PipelineMonitor` instance held on the
agent (``agent.pipeline_monitor``) — invoking the tool and then the slash
command in the same session sees consistent state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from deile.orchestration.pipeline.constants import PIPELINE_DEFAULT_REPO
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)


def _resolve_repo() -> str:
    from deile.config.settings import get_settings

    return get_settings().pipeline_repo or PIPELINE_DEFAULT_REPO


def _resolve_base_path() -> Path:
    from deile.config.settings import get_settings

    s = get_settings()
    if s.pipeline_base_path:
        return s.pipeline_base_path.resolve()
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").is_dir() and (ancestor / "deile.py").is_file():
            return ancestor
    return cwd


class PipelineTool(Tool):
    """Start/stop/inspect the autonomous DEILE-bot → DEILE → Claude Code pipeline."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="pipeline",
                description=(
                    "Control the autonomous pipeline that polls GitHub issues/PRs and "
                    "delegates to Claude Code one-shot for implementation/review. "
                    "Use action='start' to begin the 1-minute polling loop, "
                    "action='stop' to halt it, action='status' for ticks/reviewed/"
                    "implemented/PRs/errors counters, action='tick' to force a single "
                    "synchronous tick (debug)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["start", "stop", "status", "tick"],
                            "description": "Pipeline operation to perform.",
                        },
                    },
                    "required": ["action"],
                },
                required=["action"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )

    @property
    def name(self) -> str:
        return "pipeline"

    @property
    def description(self) -> str:
        return self._schema.description if self._schema else ""

    @property
    def category(self) -> str:
        return ToolCategory.SYSTEM.value

    async def execute(self, context: ToolContext) -> ToolResult:
        action = (context.parsed_args.get("action") or "status").strip().lower()
        if action not in {"start", "stop", "status", "tick"}:
            return ToolResult.error_result(
                message=f"action must be one of start|stop|status|tick, got {action!r}",
                error_code="INVALID_ACTION",
            )

        agent = context.session_data.get("agent") or context.extra.get("agent")
        monitor = self._get_or_create_monitor(agent)

        try:
            if action == "start":
                await monitor.start()
                msg = (
                    f"pipeline iniciado (repo={monitor.config.repo}, "
                    f"interval={monitor.config.poll_interval_seconds}s)"
                )
                return ToolResult.success_result(
                    data={"running": True, "repo": monitor.config.repo},
                    message=msg,
                )
            if action == "stop":
                await monitor.stop()
                return ToolResult.success_result(
                    data={"running": False}, message="pipeline parado"
                )
            if action == "tick":
                await monitor.tick()
                return ToolResult.success_result(
                    data=self._stats_dict(monitor),
                    message="single tick executed",
                )
            # status (default)
            running = self._is_running(monitor)
            return ToolResult.success_result(
                data={"running": running, **self._stats_dict(monitor)},
                message=(
                    f"pipeline {'rodando' if running else 'parado'} | "
                    f"repo={monitor.config.repo}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure to the LLM
            return ToolResult.error_result(
                message=f"pipeline {action} failed: {type(exc).__name__}: {exc}",
                error=exc,
                error_code="PIPELINE_OP_FAILED",
            )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_or_create_monitor(agent: Optional[Any]) -> PipelineMonitor:
        if agent is not None and getattr(agent, "pipeline_monitor", None) is not None:
            return agent.pipeline_monitor
        from deile.config.settings import get_settings

        cfg = PipelineConfig(
            repo=_resolve_repo(),
            base_repo_path=_resolve_base_path(),
            notify_user_id=get_settings().pipeline_notify_user_id,
        )
        monitor = PipelineMonitor(cfg)
        if agent is not None:
            try:
                agent.pipeline_monitor = monitor  # type: ignore[attr-defined]
            except Exception:
                pass
        return monitor

    @staticmethod
    def _is_running(monitor: PipelineMonitor) -> bool:
        task = getattr(monitor, "_task", None)
        return task is not None and not task.done()

    @staticmethod
    def _stats_dict(monitor: PipelineMonitor) -> dict:
        s = monitor.stats
        return {
            "ticks": s.ticks,
            "issues_reviewed": s.issues_reviewed,
            "issues_implemented": s.issues_implemented,
            "prs_reviewed": s.prs_reviewed,
            "errors": s.errors,
        }
