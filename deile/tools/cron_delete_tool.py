"""CronDeleteTool — remove or disable a scheduled prompt (intent #86)."""

from __future__ import annotations

from deile.cron.store import open_cron_store
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)


class CronDeleteTool(Tool):
    """Remove or disable a scheduled prompt by id."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="cron_delete",
                description=(
                    "Remove a scheduled prompt by id (or just disable it with "
                    "`disable_only=true` to keep its history). The id comes "
                    "from cron_create's response or cron_list."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Entry id to remove/disable.",
                        },
                        "disable_only": {
                            "type": "boolean",
                            "description": (
                                "If true, set enabled=false instead of "
                                "deleting (preserves last_result for audit)."
                            ),
                        },
                    },
                    "required": ["id"],
                },
                required=["id"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )


    async def execute(self, context: ToolContext) -> ToolResult:
        args = context.parsed_args or {}
        entry_id = (args.get("id") or "").strip()
        disable_only = bool(args.get("disable_only", False))

        if not entry_id:
            return ToolResult.error_result(
                message="id is required", error_code="MISSING_ID",
            )

        try:
            store = open_cron_store()
            if disable_only:
                ok = store.set_enabled(entry_id, False)
                action_label = "disabled"
            else:
                ok = store.remove(entry_id)
                action_label = "removed"
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error_result(
                message=f"{type(exc).__name__}: {exc}",
                error=exc, error_code="UNEXPECTED",
            )

        if not ok:
            return ToolResult.error_result(
                message=f"no entry with id={entry_id!r}",
                error_code="NOT_FOUND",
            )
        return ToolResult.success_result(
            data={"id": entry_id, "action": action_label},
            message=f"{action_label} {entry_id!r}",
        )
