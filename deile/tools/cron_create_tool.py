"""CronCreateTool — schedule a natural-language prompt for future execution.

Implements the create half of intent #86. Pass either ``cron`` (recurring)
or ``run_at`` (one-shot UTC datetime). The prompt is whatever the user
asked DEILE to do — it gets fed back into a fresh agent turn when the
schedule fires.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from deile.cron.store import CronEntry, CronStore, CronStoreError, make_id, resolve_db_path
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)


def _get_store() -> CronStore:
    return CronStore(resolve_db_path())


class CronCreateTool(Tool):
    """Schedule a prompt for future execution (recurring cron OR one-shot)."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="cron_create",
                description=(
                    "Schedule a natural-language prompt to be executed by DEILE "
                    "at a future time. Provide EITHER `cron` (5-field expression "
                    "for recurring tasks, e.g. '0 9 * * 1' = Mondays at 09:00 UTC) "
                    "OR `run_at` (ISO-8601 UTC datetime for one-shot, e.g. "
                    "'2026-05-06T18:00:00Z'). When the schedule fires, the prompt "
                    "is fed back into a fresh DEILE turn. Set `notify_user_id` to "
                    "DM the result to a Discord user."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "The natural-language instruction DEILE will "
                                "execute when the schedule fires. Should be "
                                "self-contained (no missing context)."
                            ),
                        },
                        "cron": {
                            "type": "string",
                            "description": (
                                "5-field cron expression in UTC. Use for "
                                "recurring tasks. Mutually exclusive with run_at."
                            ),
                        },
                        "run_at": {
                            "type": "string",
                            "description": (
                                "ISO-8601 UTC datetime. Use for one-shot. "
                                "Mutually exclusive with cron."
                            ),
                        },
                        "id": {
                            "type": "string",
                            "description": "Custom id (auto-generated if omitted).",
                        },
                        "notify_user_id": {
                            "type": "string",
                            "description": (
                                "Discord snowflake to DM the result to when "
                                "the schedule fires."
                            ),
                        },
                        "created_by": {
                            "type": "string",
                            "description": (
                                "Identifier of the user who scheduled this "
                                "(e.g. 'discord:1234'). Optional but useful "
                                "for audit."
                            ),
                        },
                    },
                    "required": ["prompt"],
                },
                required=["prompt"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )

    @property
    def name(self) -> str:
        return "cron_create"

    @property
    def description(self) -> str:
        return self._schema.description if self._schema else ""

    @property
    def category(self) -> str:
        return ToolCategory.SYSTEM.value

    async def execute(self, context: ToolContext) -> ToolResult:
        args = context.parsed_args or {}
        prompt = (args.get("prompt") or "").strip()
        cron = args.get("cron")
        run_at_str = args.get("run_at")

        if not prompt:
            return ToolResult.error_result(
                message="prompt is required and must be non-empty",
                error_code="MISSING_PROMPT",
            )
        if not cron and not run_at_str:
            return ToolResult.error_result(
                message="provide either cron (recurring) or run_at (one-shot)",
                error_code="MISSING_SCHEDULE",
            )
        if cron and run_at_str:
            return ToolResult.error_result(
                message="cron and run_at are mutually exclusive; provide only one",
                error_code="AMBIGUOUS_SCHEDULE",
            )

        run_at: Optional[datetime] = None
        if run_at_str:
            try:
                run_at = datetime.fromisoformat(str(run_at_str).rstrip("Z"))
            except ValueError as exc:
                return ToolResult.error_result(
                    message=f"invalid run_at: {run_at_str!r}",
                    error=exc, error_code="INVALID_DATETIME",
                )
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)

        try:
            entry = CronEntry(
                id=str(args.get("id") or make_id()),
                prompt=prompt,
                cron=cron,
                run_at=run_at,
                created_by=args.get("created_by"),
                notify_user_id=args.get("notify_user_id"),
            )
            store = _get_store()
            store.add(entry)
        except CronStoreError as exc:
            return ToolResult.error_result(
                message=str(exc), error=exc, error_code="CRON_STORE",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error_result(
                message=f"{type(exc).__name__}: {exc}",
                error=exc, error_code="UNEXPECTED",
            )

        return ToolResult.success_result(
            data={
                "id": entry.id,
                "next_fire_at": entry.next_fire_at.isoformat() if entry.next_fire_at else None,
                "is_oneshot": entry.is_oneshot,
            },
            message=(
                f"agendado {entry.id!r} — próxima execução em "
                f"{entry.next_fire_at.isoformat() if entry.next_fire_at else 'never'}"
            ),
        )
