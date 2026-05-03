"""BotTool base — extracts adapter from ToolContext.extra['bot_context']."""

from __future__ import annotations

from typing import Any, Mapping, Optional


def get_bot_context(ctx: Any) -> Mapping[str, Any]:
    extra = getattr(ctx, "extra", None) or {}
    bc = extra.get("bot_context") if isinstance(extra, Mapping) else None
    return bc or {}


def get_adapter_from_context(ctx: Any) -> Optional[Any]:
    bc = get_bot_context(ctx)
    return bc.get("adapter")


class BotTool:
    """Marker base for tools that require a bot adapter via ToolContext.extra."""

    name: str = "bot_tool_base"
    description: str = ""
    requires_bot_context: bool = True

    async def run(self, ctx: Any) -> Any:
        raise NotImplementedError
