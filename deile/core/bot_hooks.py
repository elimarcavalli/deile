"""Bot-mode hooks for DeileAgent (plano DEILE fase 2).

Provides defensive sanitization helpers for `extra_system_prompt` and
helpers to thread `bot_context` into ToolContext.extra.
"""

from __future__ import annotations

from typing import Any, Mapping

_BLOCKED_TAGS = (
    "</system>",
    "<system>",
    "</persona_override>",
    "<persona_override>",
    "</persona>",
    "<persona>",
)


def sanitize_extra_system_prompt(prompt: str) -> str:
    """Strip injection-y tags from extra_system_prompt.

    Defense in depth — foundation also sanitizes upstream. Returning empty
    string is acceptable (caller treats as no-extra).
    """
    if not prompt:
        return ""
    out = prompt
    for tag in _BLOCKED_TAGS:
        out = out.replace(tag, "")
    return out


def merge_extra_system_prompt(base: str, extra: str) -> str:
    """Append `<bot_capabilities>` block to base system prompt with separator."""
    if not extra:
        return base
    sep = "\n\n---\n"
    block = f"<bot_capabilities>\n{extra}\n</bot_capabilities>"
    return f"{base}{sep}{block}"


def get_bot_context(session: Any) -> Mapping[str, Any]:
    """Return bot_context dict from session.context_data (empty if none)."""
    if session is None:
        return {}
    ctx = getattr(session, "context_data", {})
    if not isinstance(ctx, Mapping):
        return {}
    bc = ctx.get("bot_context")
    return dict(bc) if isinstance(bc, Mapping) else {}
