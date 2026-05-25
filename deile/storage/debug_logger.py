"""Lightweight debug logger used by the model providers.

The full DEILE distribution ships a richer debug pipeline; this stub honours
the API surface (`is_debug_enabled`, `get_debug_logger().log_request/response/error`)
so the rest of the system runs without it.

Router events are written as newline-delimited JSON to `logs/router_events.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .logs import get_logger

_EVENTS_LOG = Path.home() / ".deile" / "logs" / "router_events.jsonl"


def is_debug_enabled() -> bool:
    from deile.config.settings import get_settings

    return get_settings().debug_enabled


class _DebugLogger:
    def __init__(self) -> None:
        self._logger = get_logger("debug")
        self.request_count = 0

    async def log_request(
        self,
        messages: Iterable[Any],
        metadata: Optional[Dict[str, Any]] = None,
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

    async def log_error(self, error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
        self._logger.debug("error=%s context=%s", error, context)

    async def log_router_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Append a structured router event to logs/router_events.jsonl.

        Valid event_type values:
            provider_selected, provider_call_completed, cascade_fallback,
            circuit_breaker_opened, circuit_breaker_closed, budget_exceeded
        """
        record: Dict[str, Any] = {
            "ts": time.time(),
            "event": event_type,
            **payload,
        }
        # The file write is delegated to a worker thread because this method
        # is awaited from the hot path (every provider call in agent.py).
        # A sync open() + write() here blocks the event loop on disk I/O for
        # 1-10ms per call, which compounds over a conversation. Violates
        # principle 03 §1 ("I/O bloqueante proibido em contexto async").
        try:
            await asyncio.to_thread(self._write_event_record, record)
        except OSError:
            self._logger.debug("router_event write failed: %s %s", event_type, payload)
        self._logger.debug("router_event: %s %s", event_type, payload)

    @staticmethod
    def _write_event_record(record: Dict[str, Any]) -> None:
        _EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


_singleton: Optional[_DebugLogger] = None


def get_debug_logger() -> _DebugLogger:
    global _singleton
    if _singleton is None:
        _singleton = _DebugLogger()
    return _singleton
