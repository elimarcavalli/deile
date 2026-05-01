"""Sistema de eventos enterprise-grade para DEILE 2.0 ULTRA

Implementa arquitetura event-driven com:
- Event bus central para comunicação assíncrona
- Tool lifecycle events (TOOL_INVOKED / TOOL_COMPLETED / TOOL_FAILED)
- Dead letter queue para tratamento de falhas
"""

from .event_bus import EventBus, Event, EventType, EventPriority, get_event_bus, reset_event_bus
from .event_handlers import BaseEventHandler

__all__ = [
    "EventBus",
    "Event",
    "EventType",
    "EventPriority",
    "get_event_bus",
    "reset_event_bus",
    "BaseEventHandler",
]

__version__ = "2.0.0"
