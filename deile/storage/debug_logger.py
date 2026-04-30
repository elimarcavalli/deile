"""Lightweight debug logger used by the model providers.

The full DEILE distribution ships a richer debug pipeline; this stub honours
the API surface (`is_debug_enabled`, `get_debug_logger().log_request/response/error`)
so the rest of the system runs without it.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

from .logs import get_logger


def is_debug_enabled() -> bool:
    return os.getenv("DEILE_DEBUG", "").lower() in {"1", "true", "yes", "on"}


class _DebugLogger:
    def __init__(self) -> None:
        self._logger = get_logger("debug")
        self.request_count = 0

    async def log_request(
        self,
        messages: Iterable[Any],
        metadata: Optional[dict[str, Any]] = None,
        config: Optional[Any] = None,
    ) -> None:
        self.request_count += 1
        self._logger.debug(
            "request #%s metadata=%s config=%s",
            self.request_count,
            metadata,
            config,
        )

    async def log_response(
        self,
        response: Any,
        execution_time: Optional[float] = None,
        request_id: Optional[int] = None,
    ) -> None:
        self._logger.debug(
            "response request_id=%s execution_time=%s",
            request_id,
            execution_time,
        )

    async def log_error(self, error: Exception, context: Optional[dict[str, Any]] = None) -> None:
        self._logger.debug("error=%s context=%s", error, context)


_singleton: Optional[_DebugLogger] = None


def get_debug_logger() -> _DebugLogger:
    global _singleton
    if _singleton is None:
        _singleton = _DebugLogger()
    return _singleton
