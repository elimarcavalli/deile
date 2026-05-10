"""``/pipeline-schedule`` command — manage the autonomous pipeline schedule.

Usage:
    /pipeline-schedule list
        List all recurring and oneshot schedule entries.

    /pipeline-schedule add-recurring trigger:<action> cron:<expr>
        Add a cron-driven recurring entry.
        trigger: one of ``review``, ``implement``, ``pr_review``
        cron: 5-field cron expression, e.g. ``*/5 * * * *``

    /pipeline-schedule add-oneshot trigger:<action> at:<iso>
        Add a one-time run at a specific UTC datetime.
        at: ISO-8601 UTC, e.g. ``2026-05-06T18:00:00Z``

    /pipeline-schedule remove id:<entry_id>
        Delete a recurring or oneshot entry.

    /pipeline-schedule enable id:<entry_id>
    /pipeline-schedule disable id:<entry_id>
        Toggle a recurring entry on or off.

The command delegates to :class:`PipelineScheduleTool` for all mutations so
that validation and persistence logic stay in one place (the tool).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from deile.commands.base import CommandContext, CommandResult, DirectCommand
from deile.config.manager import CommandConfig
from deile.tools.base import ToolContext
from deile.tools.pipeline_schedule_tool import PipelineScheduleTool

logger = logging.getLogger(__name__)

# Pre-compiled lookahead for the start of a new key:value pair.
# A new key is a word boundary followed by ``word_chars:``.
_KEY_START_RE = re.compile(r"\s+\w[\w-]*:")


def _parse_kv(text: str) -> dict[str, str]:
    """Parse a ``key:value key2:val2 …`` string into a dict.

    Handles multi-word values (e.g. ``cron:*/5 * * * *``) and values with
    colons (e.g. ``at:2026-05-06T18:00:00Z``) by consuming everything up to
    the next ``<space>key:`` boundary or end-of-string.

    Algorithm: split on boundaries where a space is followed by a ``word:``
    pattern, then parse each segment as ``key:rest-of-line``.
    """
    result: dict[str, str] = {}
    # Insert sentinels so the pattern can split cleanly.
    # Replace "  key:" boundaries with a null-byte separator.
    normalised = _KEY_START_RE.sub(lambda m: "\x00" + m.group(0).lstrip(), text.strip())
    for segment in normalised.split("\x00"):
        segment = segment.strip()
        if not segment:
            continue
        colon = segment.find(":")
        if colon <= 0:
            continue
        key = segment[:colon].strip()
        value = segment[colon + 1:].strip()
        if key:
            result[key] = value
    return result


class PipelineScheduleCommand(DirectCommand):
    """``/pipeline-schedule {list|add-recurring|add-oneshot|remove|enable|disable}``."""

    def __init__(self) -> None:
        super().__init__(
            CommandConfig(
                name="pipeline-schedule",
                description=(
                    "Gerencia o schedule do pipeline autônomo "
                    "(list|add-recurring|add-oneshot|remove|enable|disable)"
                ),
                action="pipeline-schedule",
            )
        )
        self.category = "orchestration"
        self._tool = PipelineScheduleTool()

    async def execute(self, context: CommandContext) -> CommandResult:
        raw = (context.args or "").strip()
        parts = raw.split(None, 1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1] if len(parts) > 1 else ""

        tool_args: dict[str, Any] = {}

        if sub == "list":
            tool_args = {"action": "list"}

        elif sub == "add-recurring":
            kv = _parse_kv(rest)
            trigger = kv.get("trigger")
            cron = kv.get("cron")
            if not trigger or not cron:
                return CommandResult.error_result(
                    "add-recurring requires trigger:<action> and cron:<expr>\n"
                    "Example: /pipeline-schedule add-recurring trigger:review cron:*/5 * * * *"
                )
            tool_args = {
                "action": "add_recurring",
                "trigger_action": trigger,
                "cron": cron,
            }
            if "id" in kv:
                tool_args["id"] = kv["id"]

        elif sub == "add-oneshot":
            kv = _parse_kv(rest)
            trigger = kv.get("trigger")
            at = kv.get("at")
            if not trigger or not at:
                return CommandResult.error_result(
                    "add-oneshot requires trigger:<action> and at:<iso-datetime>\n"
                    "Example: /pipeline-schedule add-oneshot trigger:review at:2026-05-06T18:00:00Z"
                )
            tool_args = {
                "action": "add_oneshot",
                "trigger_action": trigger,
                "run_at": at,
            }
            if "id" in kv:
                tool_args["id"] = kv["id"]

        elif sub == "remove":
            kv = _parse_kv(rest)
            entry_id = kv.get("id")
            if not entry_id:
                return CommandResult.error_result(
                    "remove requires id:<entry_id>\n"
                    "Example: /pipeline-schedule remove id:review_loop"
                )
            tool_args = {"action": "remove", "id": entry_id}

        elif sub in ("enable", "disable"):
            kv = _parse_kv(rest)
            entry_id = kv.get("id")
            if not entry_id:
                return CommandResult.error_result(
                    f"{sub} requires id:<entry_id>\n"
                    f"Example: /pipeline-schedule {sub} id:review_loop"
                )
            tool_args = {"action": sub, "id": entry_id}

        else:
            return CommandResult.error_result(
                f"Unknown sub-command: {sub!r}\n"
                "Valid sub-commands: list, add-recurring, add-oneshot, remove, enable, disable"
            )

        try:
            tool_ctx = ToolContext(user_input=raw, parsed_args=tool_args)
            result = await self._tool.execute(tool_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.error("pipeline-schedule tool error: %s", exc, exc_info=True)
            return CommandResult.error_result(
                f"{type(exc).__name__}: {exc}", error=exc
            )

        if not result.is_success:
            return CommandResult.error_result(result.message or "tool returned error")

        return CommandResult(
            success=True,
            content=_format_result(sub, result.data, result.message),
        )


# ---------------------------------------------------------------------------
# display helpers
# ---------------------------------------------------------------------------

def _format_result(sub: str, data: Any, message: str | None) -> str:
    if sub == "list":
        if not data:
            return message or "no schedule data"
        recurring = data.get("recurring", [])
        oneshot = data.get("oneshot", [])
        lines = [f"Monitor: {data.get('monitor_id', '?')}"]
        if recurring:
            lines.append("\nRecurring:")
            for e in recurring:
                status = "on" if e.get("enabled", True) else "off"
                lines.append(
                    f"  [{status}] {e['id']}  {e['action']}  @  {e['cron']}"
                )
        if oneshot:
            lines.append("\nOneshot:")
            for e in oneshot:
                lines.append(
                    f"  {e['id']}  {e['action']}  at  {e.get('run_at', '?')}"
                )
        if not recurring and not oneshot:
            lines.append("  (empty)")
        return "\n".join(lines)

    return message or str(data)
