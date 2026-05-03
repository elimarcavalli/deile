"""WebhookRouter — generic dispatcher used by HTTP-only providers."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Mapping

from deile_bot.foundation.logging import get_logger

WebhookHandler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class WebhookRouter:
    """Maps provider object names to async handlers; central HTTP dispatcher."""

    def __init__(self):
        self._handlers: Dict[str, WebhookHandler] = {}
        self._logger = get_logger("webhook_router")

    def register_object_handler(self, name: str, handler: WebhookHandler) -> None:
        if name in self._handlers:
            raise ValueError(f"handler already registered: {name}")
        self._handlers[name] = handler

    def has_handler(self, name: str) -> bool:
        return name in self._handlers

    async def dispatch(self, name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"status": "no_handler", "object": name}
        try:
            return await handler(payload)
        except Exception as e:  # noqa: BLE001
            self._logger.exception("webhook handler raised", extra={"object": name})
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    def list_handlers(self) -> list:
        return list(self._handlers.keys())
