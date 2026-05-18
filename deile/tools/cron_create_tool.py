"""CronCreateTool — schedule a natural-language prompt for future execution.

Implements the create half of intent #86. Caller may provide:
    - ``when`` (recommended): natural string parsed by
      :func:`deile.cron.parsing.parse_natural_schedule` — accepts BR humano,
      ISO ±TZ, "amanhã 9h", "hoje 23:00", or 5-field cron in UTC. Naive ISO
      and BR formats are interpreted as BRT (UTC-3).
    - ``cron`` (legacy): explicit 5-field cron in UTC.
    - ``run_at`` (legacy): explicit ISO datetime (naive treated as UTC).

When the schedule fires the prompt is fed back into a fresh DEILE turn.
``notify_user_id`` causes the runner to DM the result via the bot
control-plane (when wired).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from deile.cron.parsing import (ScheduleParseError, parse_iso_datetime,
                                parse_natural_schedule)
from deile.cron.store import (CronEntry, CronStoreError, make_id,
                              open_cron_store)
from deile.tools.base import (SecurityLevel, Tool, ToolCategory, ToolContext,
                              ToolResult, ToolSchema)


class CronCreateTool(Tool):
    """Schedule a prompt for future execution (recurring cron OR one-shot)."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="cron_create",
                description=(
                    "Schedule a natural-language prompt to be executed by DEILE "
                    "at a future time. Pass `when` with a natural-language string "
                    "and the tool figures out cron-vs-oneshot for you. Examples: "
                    "`when='amanhã 9h'`, `when='hoje 23:00'`, "
                    "`when='15/05/2026 09:30'` (BRT), `when='2026-05-15T12:30Z'` "
                    "(UTC), `when='*/5 * * * *'` (cron in UTC). When the schedule "
                    "fires the prompt is fed back into a fresh DEILE turn — write "
                    "it self-contained. Set `notify_user_id` to DM the result to "
                    "the requesting Discord user."
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
                        "when": {
                            "type": "string",
                            "description": (
                                "Natural-language schedule. Accepts: '15/05/2026 "
                                "09:30' (BRT), '15/05 09:30' (BRT, ano atual), "
                                "'amanhã 14h', 'hoje 23:00', '2026-05-15T09:30' "
                                "(ISO sem TZ — assume BRT), '2026-05-15T12:30:00Z' "
                                "(ISO em UTC), or '*/5 * * * *' (cron 5 campos "
                                "UTC). PREFERRED over cron/run_at."
                            ),
                        },
                        "cron": {
                            "type": "string",
                            "description": (
                                "Legacy: 5-field cron expression in UTC. Use "
                                "`when` instead unless caller already has a cron "
                                "string. Mutually exclusive with run_at and when."
                            ),
                        },
                        "run_at": {
                            "type": "string",
                            "description": (
                                "Legacy: ISO-8601 datetime (naive = UTC). Use "
                                "`when` instead. Mutually exclusive with cron "
                                "and when."
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
                                "for audit and ownership filtering."
                            ),
                        },
                    },
                },
                required=["prompt"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )

    async def execute(self, context: ToolContext) -> ToolResult:
        args = context.parsed_args or {}
        prompt = (args.get("prompt") or "").strip()
        when_str = (args.get("when") or "").strip() or None
        cron = args.get("cron") or None
        run_at_str = args.get("run_at") or None

        if not prompt:
            return ToolResult.error_result(
                message="prompt is required and must be non-empty",
                error_code="MISSING_PROMPT",
            )

        provided = sum(1 for v in (when_str, cron, run_at_str) if v)
        if provided == 0:
            return ToolResult.error_result(
                message=(
                    "provide `when` (preferred — natural language) OR `cron` "
                    "(5-field UTC) OR `run_at` (ISO datetime)"
                ),
                error_code="MISSING_SCHEDULE",
            )
        if provided > 1:
            return ToolResult.error_result(
                message="when, cron, and run_at are mutually exclusive; pick one",
                error_code="AMBIGUOUS_SCHEDULE",
            )

        run_at: Optional[datetime] = None

        if when_str:
            try:
                cron, run_at = parse_natural_schedule(when_str)
            except ScheduleParseError as exc:
                return ToolResult.error_result(
                    message=str(exc), error=exc, error_code="INVALID_WHEN",
                )
        elif run_at_str:
            run_at = parse_iso_datetime(str(run_at_str), naive_tz=timezone.utc)
            if run_at is None:
                return ToolResult.error_result(
                    message=f"invalid run_at: {run_at_str!r}",
                    error_code="INVALID_DATETIME",
                )

        try:
            entry = CronEntry(
                id=str(args.get("id") or make_id()),
                prompt=prompt,
                cron=cron,
                run_at=run_at,
                created_by=args.get("created_by"),
                notify_user_id=args.get("notify_user_id"),
            )
            store = open_cron_store()
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

        serialized = entry.to_dict()
        return ToolResult.success_result(
            data={
                key: serialized[key]
                for key in ("id", "next_fire_at", "is_oneshot", "cron", "run_at")
            },
            message=(
                f"agendado {entry.id!r} — próxima execução em "
                f"{serialized['next_fire_at'] or 'never'}"
            ),
        )
