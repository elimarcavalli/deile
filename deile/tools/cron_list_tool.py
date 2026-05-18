"""CronListTool — list scheduled prompts (intent #86)."""

from __future__ import annotations

from deile.cron.store import open_cron_store
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)


class CronListTool(Tool):
    """List scheduled prompts (recurring + one-shot)."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="cron_list",
                description=(
                    "List all scheduled prompts (recurring crons + one-shots). "
                    "Use this to answer 'what's scheduled?' / 'what tasks are "
                    "pending?'. Set `only_enabled=true` to skip disabled "
                    "entries. Set `created_by` to filter by who scheduled."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "only_enabled": {
                            "type": "boolean",
                            "description": "If true, omit disabled / completed entries.",
                        },
                        "created_by": {
                            "type": "string",
                            "description": "Filter by creator id (e.g. 'discord:1234').",
                        },
                    },
                    "required": [],
                },
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.SYSTEM,
            )
        )


    async def execute(self, context: ToolContext) -> ToolResult:
        args = context.parsed_args or {}
        only_enabled = bool(args.get("only_enabled", False))
        creator = args.get("created_by")
        try:
            store = open_cron_store()
            entries = store.list_all(only_enabled=only_enabled)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error_result(
                message=f"could not list cron entries: {exc}",
                error=exc, error_code="LIST_FAILED",
            )

        if creator:
            entries = [e for e in entries if e.created_by == creator]

        out = [e.to_dict() for e in entries]
        return ToolResult.success_result(
            data={"entries": out, "count": len(out)},
            message=f"{len(out)} entries scheduled",
        )
