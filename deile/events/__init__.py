"""Sistema de eventos enterprise-grade para DEILE 2.0 ULTRA

Implementa arquitetura event-driven com:
- Event bus central para comunicação assíncrona
- Event sourcing para auditoria completa
- Saga pattern para workflows complexos
- Dead letter queue para tratamento de falhas
"""

from .event_bus import EventBus, Event, EventType
from .event_store import EventStore
from .event_handlers import BaseEventHandler
from .saga_orchestrator import SagaOrchestrator
from .dead_letter_queue import DeadLetterQueue

__all__ = [
    "EventBus",
    "Event",
    "EventType",
    "EventStore",
    "BaseEventHandler",
    "SagaOrchestrator",
    "DeadLetterQueue"
]

__version__ = "2.0.0"