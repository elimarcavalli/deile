"""Handlers base e implementações específicas para eventos do sistema"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import time

from .event_bus import Event, EventType

logger = logging.getLogger(__name__)


class BaseEventHandler(ABC):
    """Classe base para handlers de eventos"""

    def __init__(self, name: str):
        self.name = name
        self.handled_events = 0
        self.failed_events = 0
        self.last_execution = 0.0

    @abstractmethod
    async def handle(self, event: Event) -> None:
        """Processa um evento específico"""
        pass

    async def __call__(self, event: Event) -> None:
        """Torna o handler callable para o event bus"""
        try:
            start_time = time.time()
            await self.handle(event)
            self.handled_events += 1
            self.last_execution = time.time()

            logger.debug(f"Handler {self.name} processou evento {event.event_id} em {time.time() - start_time:.3f}s")
        except Exception as e:
            self.failed_events += 1
            logger.error(f"Handler {self.name} falhou ao processar evento {event.event_id}: {e}")
            raise

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do handler"""
        total = self.handled_events + self.failed_events
        success_rate = (self.handled_events / total * 100) if total > 0 else 0

        return {
            "name": self.name,
            "handled_events": self.handled_events,
            "failed_events": self.failed_events,
            "success_rate": success_rate,
            "last_execution": self.last_execution
        }


class SystemEventHandler(BaseEventHandler):
    """Handler para eventos de sistema"""

    def __init__(self):
        super().__init__("SystemEventHandler")

    async def handle(self, event: Event) -> None:
        """Processa eventos de sistema"""
        if event.event_type == EventType.SYSTEM_STARTED:
            logger.info("Sistema DEILE iniciado")

        elif event.event_type == EventType.SYSTEM_STOPPED:
            logger.info("Sistema DEILE parado")


class PersonaEventHandler(BaseEventHandler):
    """Handler para eventos de persona"""

    def __init__(self):
        super().__init__("PersonaEventHandler")

    async def handle(self, event: Event) -> None:
        """Processa eventos de persona"""
        if event.event_type == EventType.PERSONA_ACTIVATED:
            persona_name = event.data.get("persona_name", "Unknown")
            logger.info(f"Persona '{persona_name}' ativada")

        elif event.event_type == EventType.PERSONA_DEACTIVATED:
            persona_name = event.data.get("persona_name", "Unknown")
            logger.info(f"Persona '{persona_name}' desativada")

        elif event.event_type == EventType.PERSONA_SWITCHED:
            old_persona = event.data.get("old_persona", "Unknown")
            new_persona = event.data.get("new_persona", "Unknown")
            logger.info(f"Persona alterada: {old_persona} -> {new_persona}")


class TaskEventHandler(BaseEventHandler):
    """Handler para eventos de tarefas"""

    def __init__(self):
        super().__init__("TaskEventHandler")
        self.active_tasks: Dict[str, Dict[str, Any]] = {}

    async def handle(self, event: Event) -> None:
        """Processa eventos de tarefas"""
        task_id = event.data.get("task_id")

        if event.event_type == EventType.TASK_CREATED:
            task_name = event.data.get("task_name", "Unknown")
            self.active_tasks[task_id] = {
                "name": task_name,
                "created_at": event.timestamp,
                "status": "created"
            }
            logger.info(f"Tarefa criada: {task_name} (ID: {task_id})")

        elif event.event_type == EventType.TASK_STARTED:
            if task_id in self.active_tasks:
                self.active_tasks[task_id]["status"] = "running"
                self.active_tasks[task_id]["started_at"] = event.timestamp

            task_name = event.data.get("task_name", "Unknown")
            logger.info(f"Tarefa iniciada: {task_name}")

        elif event.event_type == EventType.TASK_COMPLETED:
            if task_id in self.active_tasks:
                task_info = self.active_tasks[task_id]
                task_info["status"] = "completed"
                task_info["completed_at"] = event.timestamp

                if "started_at" in task_info:
                    duration = event.timestamp - task_info["started_at"]
                    task_info["duration"] = duration
                    logger.info(f"Tarefa completada: {task_info['name']} (duração: {duration:.2f}s)")
                else:
                    logger.info(f"Tarefa completada: {task_info['name']}")

        elif event.event_type == EventType.TASK_FAILED:
            if task_id in self.active_tasks:
                self.active_tasks[task_id]["status"] = "failed"
                self.active_tasks[task_id]["failed_at"] = event.timestamp

            task_name = event.data.get("task_name", "Unknown")
            error = event.data.get("error", "No error details")
            logger.error(f"Tarefa falhou: {task_name} - {error}")

        elif event.event_type == EventType.TASK_CANCELLED:
            if task_id in self.active_tasks:
                self.active_tasks[task_id]["status"] = "cancelled"
                self.active_tasks[task_id]["cancelled_at"] = event.timestamp

            task_name = event.data.get("task_name", "Unknown")
            logger.info(f"Tarefa cancelada: {task_name}")

    def get_active_tasks(self) -> Dict[str, Dict[str, Any]]:
        """Retorna tarefas ativas"""
        return {
            task_id: info for task_id, info in self.active_tasks.items()
            if info["status"] in ["created", "running"]
        }


class CodeEventHandler(BaseEventHandler):
    """Handler para eventos relacionados a código"""

    def __init__(self):
        super().__init__("CodeEventHandler")
        self.code_operations = []

    async def handle(self, event: Event) -> None:
        """Processa eventos de código"""
        if event.event_type == EventType.CODE_GENERATED:
            file_path = event.data.get("file_path", "Unknown")
            lines_count = event.data.get("lines_count", 0)
            logger.info(f"Código gerado: {file_path} ({lines_count} linhas)")

            self.code_operations.append({
                "type": "generated",
                "file_path": file_path,
                "lines_count": lines_count,
                "timestamp": event.timestamp
            })

        elif event.event_type == EventType.CODE_EXECUTED:
            file_path = event.data.get("file_path", "Unknown")
            success = event.data.get("success", False)
            execution_time = event.data.get("execution_time", 0)

            if success:
                logger.info(f"Código executado com sucesso: {file_path} ({execution_time:.3f}s)")
            else:
                error = event.data.get("error", "Unknown error")
                logger.error(f"Falha na execução: {file_path} - {error}")

        elif event.event_type == EventType.CODE_TESTED:
            file_path = event.data.get("file_path", "Unknown")
            tests_passed = event.data.get("tests_passed", 0)
            tests_failed = event.data.get("tests_failed", 0)

            logger.info(f"Testes executados para {file_path}: {tests_passed} passou, {tests_failed} falhou")

        elif event.event_type == EventType.FILE_MODIFIED:
            file_path = event.data.get("file_path", "Unknown")
            operation = event.data.get("operation", "modified")
            logger.info(f"Arquivo {operation}: {file_path}")


class PerformanceEventHandler(BaseEventHandler):
    """Handler para eventos de performance e monitoramento"""

    def __init__(self):
        super().__init__("PerformanceEventHandler")
        self.performance_metrics = []

    async def handle(self, event: Event) -> None:
        """Processa eventos de performance"""
        if event.event_type == EventType.PERFORMANCE_ANALYZED:
            component = event.data.get("component", "Unknown")
            metrics = event.data.get("metrics", {})

            self.performance_metrics.append({
                "component": component,
                "metrics": metrics,
                "timestamp": event.timestamp
            })

            logger.info(f"Performance analisada para {component}: {metrics}")

        elif event.event_type == EventType.IMPROVEMENT_IDENTIFIED:
            improvement_type = event.data.get("type", "Unknown")
            description = event.data.get("description", "No description")
            impact = event.data.get("impact", "Unknown")

            logger.info(f"Melhoria identificada ({improvement_type}): {description} - Impacto: {impact}")

        elif event.event_type == EventType.IMPROVEMENT_APPLIED:
            improvement_id = event.data.get("improvement_id", "Unknown")
            success = event.data.get("success", False)

            if success:
                logger.info(f"Melhoria aplicada com sucesso: {improvement_id}")
            else:
                error = event.data.get("error", "Unknown error")
                logger.error(f"Falha ao aplicar melhoria {improvement_id}: {error}")


class ErrorEventHandler(BaseEventHandler):
    """Handler especializado para eventos de erro"""

    def __init__(self):
        super().__init__("ErrorEventHandler")
        self.error_history = []
        self.max_history = 1000

    async def handle(self, event: Event) -> None:
        """Processa eventos de erro"""
        error_info = {
            "timestamp": event.timestamp,
            "source": event.source,
            "error_type": event.data.get("error_type", "Unknown"),
            "error_message": event.data.get("error_message", "No message"),
            "stack_trace": event.data.get("stack_trace"),
            "context": event.data.get("context", {}),
            "severity": "critical" if event.event_type == EventType.CRITICAL_ERROR else "normal"
        }

        self.error_history.append(error_info)

        # Mantém histórico limitado
        if len(self.error_history) > self.max_history:
            self.error_history = self.error_history[-self.max_history:]

        # Log appropriado baseado na severidade
        if event.event_type == EventType.CRITICAL_ERROR:
            logger.critical(f"ERRO CRÍTICO em {event.source}: {error_info['error_message']}")
        else:
            logger.error(f"Erro em {event.source}: {error_info['error_message']}")

        # Para erros críticos, pode implementar notificações adicionais
        if event.event_type == EventType.CRITICAL_ERROR:
            await self._handle_critical_error(error_info)

    async def _handle_critical_error(self, error_info: Dict[str, Any]) -> None:
        """Tratamento especial para erros críticos"""
        # Implementar notificações, alertas, etc.
        logger.critical("Sistema em estado crítico - implementar recuperação automática")

    def get_error_summary(self) -> Dict[str, Any]:
        """Retorna resumo dos erros"""
        if not self.error_history:
            return {"total_errors": 0, "critical_errors": 0, "recent_errors": []}

        total_errors = len(self.error_history)
        critical_errors = sum(1 for e in self.error_history if e["severity"] == "critical")
        recent_errors = self.error_history[-10:]  # Últimos 10 erros

        return {
            "total_errors": total_errors,
            "critical_errors": critical_errors,
            "recent_errors": recent_errors,
            "error_rate_per_hour": self._calculate_error_rate()
        }

    def _calculate_error_rate(self) -> float:
        """Calcula taxa de erro por hora"""
        if not self.error_history:
            return 0.0

        current_time = time.time()
        one_hour_ago = current_time - 3600

        recent_errors = [e for e in self.error_history if e["timestamp"] >= one_hour_ago]
        return len(recent_errors)


class AuditEventHandler(BaseEventHandler):
    """Handler para auditoria e logging de todas as operações"""

    def __init__(self):
        super().__init__("AuditEventHandler")
        self.audit_log = []
        self.max_audit_entries = 10000

    async def handle(self, event: Event) -> None:
        """Registra todos os eventos para auditoria"""
        audit_entry = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp,
            "source": event.source,
            "data": event.data,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id
        }

        self.audit_log.append(audit_entry)

        # Mantém log limitado
        if len(self.audit_log) > self.max_audit_entries:
            self.audit_log = self.audit_log[-self.max_audit_entries:]

        # Log detalhado apenas para eventos importantes
        important_events = [
            EventType.TASK_CREATED,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.CODE_GENERATED,
            EventType.FILE_MODIFIED,
            EventType.ERROR_OCCURRED,
            EventType.CRITICAL_ERROR
        ]

        if event.event_type in important_events:
            logger.debug(f"AUDIT: {event.event_type.value} por {event.source}")

    def get_audit_trail(self, limit: int = 100) -> list:
        """Retorna trilha de auditoria"""
        return self.audit_log[-limit:] if limit else self.audit_log