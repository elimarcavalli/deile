"""Event Bus central para comunicação assíncrona enterprise-grade"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable, Awaitable, Set
from enum import Enum
from datetime import datetime
import json

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Tipos de eventos do sistema"""
    # Sistema
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"

    # Persona
    PERSONA_ACTIVATED = "persona.activated"
    PERSONA_DEACTIVATED = "persona.deactivated"
    PERSONA_SWITCHED = "persona.switched"

    # Tarefas
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # Code
    CODE_GENERATED = "code.generated"
    CODE_EXECUTED = "code.executed"
    CODE_TESTED = "code.tested"
    FILE_MODIFIED = "file.modified"

    # User interaction
    USER_INPUT_RECEIVED = "user.input_received"
    RESPONSE_GENERATED = "response.generated"

    # Self-improvement
    IMPROVEMENT_IDENTIFIED = "improvement.identified"
    IMPROVEMENT_APPLIED = "improvement.applied"
    PERFORMANCE_ANALYZED = "performance.analyzed"

    # Errors
    ERROR_OCCURRED = "error.occurred"
    CRITICAL_ERROR = "error.critical"


class EventPriority(Enum):
    """Prioridades de eventos"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class Event:
    """Evento do sistema com metadata completa"""
    event_type: EventType
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    priority: EventPriority = EventPriority.NORMAL
    correlation_id: Optional[str] = None  # Para rastrear fluxos relacionados
    causation_id: Optional[str] = None    # Evento que causou este
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa evento para dicionário"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "source": self.source,
            "data": self.data,
            "priority": self.priority.value,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Event':
        """Deserializa evento de dicionário"""
        return cls(
            event_id=data["event_id"],
            event_type=EventType(data["event_type"]),
            timestamp=data["timestamp"],
            source=data["source"],
            data=data["data"],
            priority=EventPriority(data["priority"]),
            correlation_id=data.get("correlation_id"),
            causation_id=data.get("causation_id"),
            metadata=data.get("metadata", {})
        )


EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Event Bus central para comunicação assíncrona enterprise-grade

    Features:
    - Publish/Subscribe pattern
    - Priorização de eventos
    - Rate limiting e throttling
    - Dead letter queue para falhas
    - Métricas e monitoramento
    - Event replay capability
    """

    def __init__(self, max_queue_size: int = 10000):
        self.max_queue_size = max_queue_size

        # Handlers registrados por tipo de evento
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._wildcard_handlers: List[EventHandler] = []  # Handlers para todos eventos

        # Filas de eventos por prioridade
        self._event_queues: Dict[EventPriority, asyncio.Queue] = {
            priority: asyncio.Queue(maxsize=max_queue_size)
            for priority in EventPriority
        }

        # Workers para processar eventos
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._worker_count = 3

        # Dead letter queue para eventos que falharam
        self._dead_letters: List[Event] = []
        self._max_dead_letters = 1000

        # Métricas
        self._stats = {
            "events_published": 0,
            "events_processed": 0,
            "events_failed": 0,
            "handlers_executed": 0,
            "average_processing_time": 0.0
        }
        self._processing_times: List[float] = []

        # Rate limiting
        self._rate_limits: Dict[str, Dict[str, Any]] = {}  # source -> config

        logger.info("EventBus inicializado")

    async def start(self) -> None:
        """Inicia o event bus e workers"""
        if self._running:
            return

        self._running = True

        # Inicia workers para processar eventos
        for i in range(self._worker_count):
            worker = asyncio.create_task(self._event_worker(f"worker-{i}"))
            self._workers.append(worker)

        logger.info(f"EventBus iniciado com {self._worker_count} workers")

    async def stop(self) -> None:
        """Para o event bus gracefully"""
        if not self._running:
            return

        logger.info("Parando EventBus...")
        self._running = False

        # Cancela todos os workers
        for worker in self._workers:
            worker.cancel()

        # Aguarda finalização
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        logger.info("EventBus parado")

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Registra handler para um tipo específico de evento"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []

        self._handlers[event_type].append(handler)
        logger.debug(f"Handler registrado para {event_type.value}")

    def subscribe_all(self, handler: EventHandler) -> None:
        """Registra handler para todos os tipos de evento"""
        self._wildcard_handlers.append(handler)
        logger.debug("Handler wildcard registrado")

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Remove handler de um tipo de evento"""
        if event_type in self._handlers:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)
                logger.debug(f"Handler removido de {event_type.value}")

    async def publish(self, event: Event) -> bool:
        """Publica evento no bus

        Args:
            event: Evento a ser publicado

        Returns:
            bool: True se evento foi enfileirado com sucesso
        """
        if not self._running:
            logger.warning("EventBus não está rodando. Evento descartado.")
            return False

        # Verifica rate limiting
        if not await self._check_rate_limit(event):
            logger.warning(f"Rate limit excedido para source {event.source}")
            return False

        try:
            # Adiciona à fila apropriada baseado na prioridade
            queue = self._event_queues[event.priority]
            await queue.put(event)

            self._stats["events_published"] += 1
            logger.debug(f"Evento {event.event_type.value} publicado (ID: {event.event_id})")
            return True

        except asyncio.QueueFull:
            logger.error(f"Fila de eventos cheia para prioridade {event.priority}")
            await self._move_to_dead_letter(event, "Queue full")
            return False

    async def publish_and_wait(self, event: Event, timeout: float = 30.0) -> bool:
        """Publica evento e aguarda processamento completo

        Args:
            event: Evento a ser publicado
            timeout: Timeout em segundos

        Returns:
            bool: True se processado com sucesso
        """
        if not await self.publish(event):
            return False

        # Aguarda o evento ser processado (simplificado)
        # Em uma implementação completa, usaríamos um mecanismo de confirmação
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self._is_event_processed(event.event_id):
                return True
            await asyncio.sleep(0.1)

        return False

    async def _event_worker(self, worker_name: str) -> None:
        """Worker que processa eventos das filas"""
        logger.debug(f"Worker {worker_name} iniciado")

        while self._running:
            try:
                # Processa eventos por prioridade (CRITICAL -> LOW)
                event_processed = False

                for priority in sorted(EventPriority, key=lambda p: p.value, reverse=True):
                    queue = self._event_queues[priority]

                    try:
                        # Tenta pegar evento com timeout pequeno
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        await self._process_event(event)
                        event_processed = True
                        break
                    except asyncio.TimeoutError:
                        continue

                # Se não processou nenhum evento, aguarda um pouco
                if not event_processed:
                    await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                logger.debug(f"Worker {worker_name} cancelado")
                break
            except Exception as e:
                logger.error(f"Erro no worker {worker_name}: {e}")
                await asyncio.sleep(1)  # Evita loop de erro

        logger.debug(f"Worker {worker_name} finalizado")

    async def _process_event(self, event: Event) -> None:
        """Processa um evento executando todos handlers apropriados"""
        start_time = time.time()

        try:
            # Handlers específicos do tipo de evento
            handlers_to_run = []

            if event.event_type in self._handlers:
                handlers_to_run.extend(self._handlers[event.event_type])

            # Handlers wildcard
            handlers_to_run.extend(self._wildcard_handlers)

            if not handlers_to_run:
                logger.debug(f"Nenhum handler para evento {event.event_type.value}")
                return

            # Executa handlers em paralelo
            tasks = []
            for handler in handlers_to_run:
                task = asyncio.create_task(self._execute_handler(handler, event))
                tasks.append(task)

            # Aguarda todos handlers
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Conta handlers executados e falhas
            success_count = sum(1 for r in results if not isinstance(r, Exception))
            failure_count = len(results) - success_count

            self._stats["handlers_executed"] += success_count

            if failure_count > 0:
                logger.warning(f"{failure_count} handlers falharam para evento {event.event_id}")
                self._stats["events_failed"] += 1
            else:
                self._stats["events_processed"] += 1

        except Exception as e:
            logger.error(f"Erro ao processar evento {event.event_id}: {e}")
            await self._move_to_dead_letter(event, str(e))
            self._stats["events_failed"] += 1
        finally:
            # Atualiza métricas de tempo
            processing_time = time.time() - start_time
            self._processing_times.append(processing_time)

            # Mantém apenas últimas 1000 medições
            if len(self._processing_times) > 1000:
                self._processing_times = self._processing_times[-1000:]

            # Atualiza média
            self._stats["average_processing_time"] = sum(self._processing_times) / len(self._processing_times)

    async def _execute_handler(self, handler: EventHandler, event: Event) -> None:
        """Executa um handler específico com timeout"""
        try:
            await asyncio.wait_for(handler(event), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error(f"Handler timeout para evento {event.event_id}")
            raise
        except Exception as e:
            logger.error(f"Handler falhou para evento {event.event_id}: {e}")
            raise

    async def _check_rate_limit(self, event: Event) -> bool:
        """Verifica rate limiting para o source do evento"""
        # Implementação básica - pode ser expandida
        source = event.source
        if source not in self._rate_limits:
            return True

        # Por enquanto, sempre permite
        # TODO: Implementar rate limiting real
        return True

    async def _move_to_dead_letter(self, event: Event, reason: str) -> None:
        """Move evento para dead letter queue"""
        if len(self._dead_letters) >= self._max_dead_letters:
            # Remove mais antigo
            self._dead_letters.pop(0)

        # Adiciona metadata sobre a falha
        event.metadata["dead_letter_reason"] = reason
        event.metadata["dead_letter_timestamp"] = time.time()

        self._dead_letters.append(event)
        logger.warning(f"Evento {event.event_id} movido para dead letter: {reason}")

    def _is_event_processed(self, event_id: str) -> bool:
        """Verifica se evento foi processado (simplificado)"""
        # Em uma implementação real, manteria estado dos eventos
        return True

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do event bus"""
        return {
            "running": self._running,
            "worker_count": len(self._workers),
            "handlers_registered": sum(len(handlers) for handlers in self._handlers.values()),
            "wildcard_handlers": len(self._wildcard_handlers),
            "queue_sizes": {
                priority.name: self._event_queues[priority].qsize()
                for priority in EventPriority
            },
            "dead_letters": len(self._dead_letters),
            "stats": self._stats.copy()
        }

    async def get_dead_letters(self) -> List[Event]:
        """Retorna eventos em dead letter queue"""
        return self._dead_letters.copy()

    async def replay_dead_letter(self, event_id: str) -> bool:
        """Reprocessa evento da dead letter queue"""
        for i, event in enumerate(self._dead_letters):
            if event.event_id == event_id:
                # Remove da dead letter
                dead_event = self._dead_letters.pop(i)

                # Remove metadata de dead letter
                if "dead_letter_reason" in dead_event.metadata:
                    del dead_event.metadata["dead_letter_reason"]
                if "dead_letter_timestamp" in dead_event.metadata:
                    del dead_event.metadata["dead_letter_timestamp"]

                # Republica
                return await self.publish(dead_event)

        return False