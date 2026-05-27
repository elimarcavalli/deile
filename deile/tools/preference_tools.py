"""Function-call tools for managing user preferences.

Three tools — ``remember_preference``, ``list_preferences``,
``forget_preference`` — backed by :class:`~deile.preferences.store.PreferenceStore`.
Auto-discovery picks them up via ``DEFAULT_TOOL_PACKAGES``.

Security: writes pass through :class:`~deile.security.permissions.PermissionManager`
before touching disk (fail-closed, aligned with ``settings_write``). Secrets
and PII must never be stored — the tool description warns LLMs of this.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..preferences.store import PreferenceStore
from .base import (
    SecurityLevel,
    Tool,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSchema,
)

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────


def _resolve_user_id(context: ToolContext) -> str:
    """Best-effort extract a user_id from the tool context.

    Probes ``session_data``, then ``metadata``, then ``extra``. Falls back
    to ``"unknown"`` when no id is discoverable (tests and unattended
    environments).
    """
    for source in (context.session_data, context.metadata, context.extra):
        uid = source.get("user_id") if isinstance(source, dict) else None
        if uid:
            return str(uid)
    return "unknown"


def _check_write_permission(
    context: ToolContext, tool_name: str, key: str
) -> bool:
    """Consult PermissionManager before writing. Fail-closed.

    *tool_name* is the invoking tool (``remember_preference`` or
    ``forget_preference``) — passed through so operators can write rules
    that differentiate the two (e.g. allow remember, deny forget).
    """
    try:
        from ..security.permissions import get_permission_manager

        pm = get_permission_manager()
    except Exception:
        logger.exception("preference_tools: cannot resolve PermissionManager")
        return False
    if pm is None:
        logger.warning("preference_tools: no PermissionManager — denying write")
        return False
    resource = f"preferences:{_resolve_user_id(context)}:{key}"
    try:
        return bool(
            pm.check_permission(
                tool_name=tool_name,
                resource=resource,
                action="write",
                context={"key": key},
            )
        )
    except Exception:
        logger.exception("preference_tools: PermissionManager raised — denying")
        return False


# ── Tool definitions ─────────────────────────────────────────────────────


class RememberPreferenceTool(Tool):
    """Persist a user preference (key + value)."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="remember_preference",
                description=(
                    "Store a user preference as a key-value pair. "
                    "Preferences persist across sessions in "
                    "~/.deile/preferences.json. Use this when the user says "
                    "'remember that I prefer…', 'save this setting…', or "
                    "'always do X for me'. "
                    "⚠️ NEVER store secrets, tokens, passwords, API keys "
                    "or PII. This is for preferences only, NOT secrets. "
                    "Keys must be snake_case (a-z, 0-9, _, .) starting "
                    "with a letter, max 128 chars. Values must be string, "
                    "number, boolean, or null, max 4096 chars."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": (
                                "Preference key in snake_case with optional "
                                "dot-namespace (e.g. 'subagents.mode', "
                                "'ui.theme'). Must match [a-z][a-z0-9_.]{0,127}."
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "Value to store. Pass booleans and numbers "
                                "as their string representation (e.g. 'true', "
                                "'42'). Max 4096 chars."
                            ),
                        },
                    },
                    "required": ["key", "value"],
                },
                required=["key", "value"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )
        self._store = PreferenceStore()

    async def execute(self, context: ToolContext) -> ToolResult:
        key = context.parsed_args.get("key")
        value = context.parsed_args.get("value")

        if not key or not isinstance(key, str):
            return ToolResult.error_result(
                message="remember_preference requires a non-empty `key` argument."
            )
        if value is None:
            return ToolResult.error_result(
                message="remember_preference requires a `value` argument."
            )

        user_id = _resolve_user_id(context)

        # Coerce booleans and numbers
        coerced_value = _coerce_value(value)

        # Validate
        from ..preferences.store import _validate_key, _validate_value

        try:
            _validate_key(key)
            _validate_value(coerced_value)
        except ValueError as exc:
            return ToolResult.error_result(message=str(exc))

        if not _check_write_permission(context, "remember_preference", key):
            return ToolResult.error_result(
                message=(
                    "Permission denied: preference writes are not enabled. "
                    "Ask the operator to add a 'preferences_write' rule in "
                    "config/permissions.yaml."
                ),
                error_code="PERMISSION_DENIED",
            )

        try:
            self._store.store(user_id, key, coerced_value)
        except Exception as exc:
            return ToolResult.error_result(
                message=f"Failed to store preference '{key}': {exc}"
            )

        return ToolResult.success_result(
            data={"key": key, "value": coerced_value},
            message=f"Preference '{key}' stored successfully.",
        )


class ListPreferencesTool(Tool):
    """List all preferences for the current user, optionally filtered."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="list_preferences",
                description=(
                    "List all stored preferences for the current user. "
                    "Pass an optional `prefix` to filter by key prefix "
                    "(e.g. 'subagents' returns 'subagents.mode', "
                    "'subagents.count', etc.). Returns a dict of key-value "
                    "pairs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": (
                                "Optional key prefix to filter results "
                                "(e.g. 'ui', 'subagents')."
                            ),
                        },
                    },
                },
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.SYSTEM,
            )
        )
        self._store = PreferenceStore()

    async def execute(self, context: ToolContext) -> ToolResult:
        user_id = _resolve_user_id(context)
        prefix = context.parsed_args.get("prefix")

        try:
            all_prefs = self._store.get_all(user_id)
        except Exception as exc:
            return ToolResult.error_result(
                message=f"Failed to read preferences: {exc}"
            )

        if prefix and isinstance(prefix, str):
            filtered: Dict[str, Any] = {}
            for k, v in all_prefs.items():
                if k.startswith(prefix):
                    filtered[k] = v
            all_prefs = filtered

        if not all_prefs:
            return ToolResult.success_result(
                data={"preferences": {}},
                message="No preferences stored yet.",
            )

        lines = "\n".join(f"  {k}: {v!r}" for k, v in sorted(all_prefs.items()))
        return ToolResult.success_result(
            data={"preferences": all_prefs},
            message=f"Preferences:\n{lines}",
        )


class ForgetPreferenceTool(Tool):
    """Remove a user preference by key. Idempotent."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="forget_preference",
                description=(
                    "Delete a stored preference by key. Idempotent — "
                    "removing a key that doesn't exist succeeds silently. "
                    "Use this when the user says 'forget that I said…', "
                    "'remove the preference…', or 'stop always doing X'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Preference key to remove.",
                        },
                    },
                    "required": ["key"],
                },
                required=["key"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
            )
        )
        self._store = PreferenceStore()

    async def execute(self, context: ToolContext) -> ToolResult:
        key = context.parsed_args.get("key")
        if not key or not isinstance(key, str):
            return ToolResult.error_result(
                message="forget_preference requires a non-empty `key` argument."
            )

        user_id = _resolve_user_id(context)

        if not _check_write_permission(context, "forget_preference", key):
            return ToolResult.error_result(
                message=(
                    "Permission denied: preference writes are not enabled. "
                    "Ask the operator to add a 'preferences_write' rule in "
                    "config/permissions.yaml."
                ),
                error_code="PERMISSION_DENIED",
            )

        try:
            existed = self._store.delete(user_id, key)
        except Exception as exc:
            return ToolResult.error_result(
                message=f"Failed to delete preference '{key}': {exc}"
            )

        if existed:
            return ToolResult.success_result(
                data={"key": key, "deleted": True},
                message=f"Preference '{key}' removed.",
            )
        return ToolResult.success_result(
            data={"key": key, "deleted": False},
            message=f"Preference '{key}' did not exist (no-op).",
        )


# ── Value coercion ───────────────────────────────────────────────────────


def _coerce_value(raw: str) -> Any:
    """Try to infer the intended type of *raw* (a string from LLM output).

    - ``"true"`` / ``"false"`` → bool
    - integer/float strings → number
    - ``"null"`` / ``"none"`` → None
    - anything else stays as string
    """
    if not isinstance(raw, str):
        return raw
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        pass
    try:
        return float(raw)
    except (ValueError, TypeError):
        pass
    return raw
