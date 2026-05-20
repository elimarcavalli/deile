"""PipelineScheduleTool — LLM-callable schedule editor for the pipeline.

Lets DEILE answer requests like:

- "agenda implementação da issue #99 pra hoje 18h"
- "muda o intervalo de review pra cada 10 minutos"
- "lista os agendamentos"
- "remove o oneshot 99"

The tool delegates to :class:`ScheduleStore` and persists changes to
``config/pipeline_schedule_<monitor_id>.yaml``. Edits take effect on
the next monitor tick.
"""

from __future__ import annotations

from datetime import datetime, timezone

from deile.orchestration.pipeline.actions import ACTION_NAMES
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.scheduler import (OneshotEntry,
                                                    RecurringEntry,
                                                    ScheduleError,
                                                    ScheduleStore)
from deile.tools._pipeline_paths import resolve_base_path as _resolve_base_path
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)


class PipelineScheduleTool(Tool):
    """List/add/remove pipeline schedule entries (recurring crons + oneshots)."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="pipeline_schedule",
                description=(
                    "Manage the autonomous pipeline schedule (cron-based "
                    "recurring entries + ad-hoc oneshots). Use action='list' "
                    "to see all entries, action='add_recurring' for cron "
                    "schedules, action='add_oneshot' for one-time runs at a "
                    "specific UTC datetime, action='remove' to delete by id, "
                    "action='enable'/'disable' to toggle a recurring entry."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "add_recurring", "add_oneshot",
                                     "remove", "enable", "disable"],
                            "description": "Operation to perform.",
                        },
                        "id": {
                            "type": "string",
                            "description": (
                                "Entry id (required for remove/enable/disable; "
                                "auto-generated if omitted on add_*)."
                            ),
                        },
                        "trigger_action": {
                            "type": "string",
                            "enum": list(ACTION_NAMES),
                            "description": "Pipeline action this entry fires.",
                        },
                        "cron": {
                            "type": "string",
                            "description": "5-field cron expression (e.g. '*/5 * * * *').",
                        },
                        "run_at": {
                            "type": "string",
                            "description": (
                                "ISO-8601 UTC datetime for oneshot (e.g. "
                                "'2026-05-06T18:00:00Z')."
                            ),
                        },
                        "target_issue": {
                            "type": "integer",
                            "description": "Issue number for oneshot (optional context).",
                        },
                        "target_pr": {
                            "type": "integer",
                            "description": "PR number for oneshot (optional context).",
                        },
                        "monitor_id": {
                            "type": "string",
                            "description": (
                                "Monitor whose schedule to edit (defaults to env "
                                "DEILE_PIPELINE_MONITOR_ID or 'default')."
                            ),
                        },
                    },
                    "required": ["action"],
                },
                required=["action"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )


    async def execute(self, context: ToolContext) -> ToolResult:
        args = context.parsed_args or {}
        action = (args.get("action") or "").strip().lower()

        identity = MonitorIdentity(
            monitor_id=args.get("monitor_id") or "default",
        )
        store = ScheduleStore(_resolve_base_path(), monitor_id=identity.monitor_id)

        try:
            schedule = store.load()
        except ScheduleError as exc:
            return ToolResult.error_result(
                message=f"could not load schedule: {exc}",
                error=exc, error_code="SCHEDULE_LOAD",
            )

        try:
            if action == "list":
                return ToolResult.success_result(
                    data={
                        "monitor_id": identity.monitor_id,
                        "recurring": [e.to_dict() for e in schedule.recurring],
                        "oneshot": [e.to_dict() for e in schedule.oneshot],
                    },
                    message=(
                        f"{len(schedule.recurring)} recurring + "
                        f"{len(schedule.oneshot)} oneshot entries"
                    ),
                )

            if action == "add_recurring":
                trigger = args.get("trigger_action")
                cron = args.get("cron")
                if not trigger or not cron:
                    return ToolResult.error_result(
                        message="add_recurring requires 'trigger_action' and 'cron'",
                        error_code="MISSING_ARGS",
                    )
                entry_id = args.get("id") or f"{trigger}_loop"
                schedule.add_recurring(RecurringEntry(
                    id=entry_id, action=trigger, cron=cron, enabled=True,
                ))
                store.save(schedule)
                return ToolResult.success_result(
                    data={"id": entry_id, "cron": cron, "action": trigger},
                    message=f"added recurring {entry_id!r} ({trigger} @ {cron})",
                )

            if action == "add_oneshot":
                trigger = args.get("trigger_action")
                run_at_str = args.get("run_at")
                if not trigger or not run_at_str:
                    return ToolResult.error_result(
                        message="add_oneshot requires 'trigger_action' and 'run_at'",
                        error_code="MISSING_ARGS",
                    )
                try:
                    run_at = datetime.fromisoformat(run_at_str.rstrip("Z"))
                except ValueError as exc:
                    return ToolResult.error_result(
                        message=f"invalid run_at: {run_at_str!r}",
                        error=exc, error_code="INVALID_DATETIME",
                    )
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=timezone.utc)
                entry_id = (
                    args.get("id")
                    or f"oneshot-{trigger}-{int(run_at.timestamp())}"
                )
                schedule.add_oneshot(OneshotEntry(
                    id=entry_id, action=trigger, run_at=run_at,
                    target_issue=args.get("target_issue"),
                    target_pr=args.get("target_pr"),
                ))
                store.save(schedule)
                return ToolResult.success_result(
                    data={
                        "id": entry_id,
                        "run_at": run_at.isoformat(),
                        "action": trigger,
                    },
                    message=f"added oneshot {entry_id!r} ({trigger} at {run_at.isoformat()})",
                )

            if action == "remove":
                entry_id = args.get("id")
                if not entry_id:
                    return ToolResult.error_result(
                        message="remove requires 'id'", error_code="MISSING_ARGS",
                    )
                if not schedule.remove(entry_id):
                    return ToolResult.error_result(
                        message=f"no entry with id={entry_id!r}",
                        error_code="NOT_FOUND",
                    )
                store.save(schedule)
                return ToolResult.success_result(
                    data={"id": entry_id}, message=f"removed {entry_id!r}",
                )

            if action in ("enable", "disable"):
                entry_id = args.get("id")
                if not entry_id:
                    return ToolResult.error_result(
                        message=f"{action} requires 'id'", error_code="MISSING_ARGS",
                    )
                entry = schedule.get_recurring(entry_id)
                if entry is None:
                    return ToolResult.error_result(
                        message=f"no recurring entry with id={entry_id!r}",
                        error_code="NOT_FOUND",
                    )
                entry.enabled = (action == "enable")
                store.save(schedule)
                return ToolResult.success_result(
                    data={"id": entry_id, "enabled": entry.enabled},
                    message=f"{entry_id} {'enabled' if entry.enabled else 'disabled'}",
                )

            return ToolResult.error_result(
                message=f"unknown action: {action!r}", error_code="INVALID_ACTION",
            )
        except ScheduleError as exc:
            return ToolResult.error_result(
                message=str(exc), error=exc, error_code="SCHEDULE_ERROR",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error_result(
                message=f"{type(exc).__name__}: {exc}",
                error=exc, error_code="UNEXPECTED",
            )
