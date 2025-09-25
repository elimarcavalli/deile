"""Task Manager - Sistema de TODO lists e validação de execução sequencial"""

from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
import json
import asyncio
import uuid
import logging
from pathlib import Path

from ..core.exceptions import DEILEError

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Status de uma task"""
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class TaskPriority(Enum):
    """Prioridade de uma task"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Task:
    """Representa uma task individual"""
    id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority = TaskPriority.MEDIUM

    # Dependências
    depends_on: List[str] = field(default_factory=list)
    blocks: List[str] = field(default_factory=list)

    # Timing
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    estimated_duration: Optional[timedelta] = None

    # Contexto e metadados
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Resultado
    success: Optional[bool] = None
    result_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict serializável"""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "depends_on": self.depends_on,
            "blocks": self.blocks,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "estimated_duration": self.estimated_duration.total_seconds() if self.estimated_duration else None,
            "tags": self.tags,
            "metadata": self.metadata,
            "success": self.success,
            "result_data": self.result_data,
            "error_message": self.error_message
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        """Cria instância a partir de dict"""
        task_data = data.copy()

        # Converte enums
        task_data["status"] = TaskStatus(data["status"])
        task_data["priority"] = TaskPriority(data["priority"])

        # Converte datetime
        task_data["created_at"] = datetime.fromisoformat(data["created_at"])
        if data.get("started_at"):
            task_data["started_at"] = datetime.fromisoformat(data["started_at"])
        if data.get("completed_at"):
            task_data["completed_at"] = datetime.fromisoformat(data["completed_at"])
        if data.get("estimated_duration"):
            task_data["estimated_duration"] = timedelta(seconds=data["estimated_duration"])

        return cls(**task_data)


@dataclass
class TaskList:
    """Representa uma lista de tasks com fluxo sequencial"""
    id: str
    title: str
    description: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    # Tasks
    tasks: List[Task] = field(default_factory=list)

    # Configurações de execução
    sequential_mode: bool = True  # Se True, executa tasks em sequência respeitando dependências
    auto_start_next: bool = True  # Se True, inicia próxima task automaticamente
    stop_on_failure: bool = True  # Se True, para execução em caso de falha

    # Status da lista
    active: bool = False
    current_task_id: Optional[str] = None

    # Estatísticas
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0

    def __post_init__(self):
        self.total_tasks = len(self.tasks)

    def add_task(self, task: Task) -> None:
        """Adiciona uma task à lista"""
        self.tasks.append(task)
        self.total_tasks = len(self.tasks)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Obtém task por ID"""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_next_tasks(self) -> List[Task]:
        """Obtém próximas tasks prontas para execução"""
        if not self.sequential_mode:
            return [task for task in self.tasks if task.status == TaskStatus.TODO]

        # Modo sequencial: verifica dependências
        ready_tasks = []

        for task in self.tasks:
            if task.status != TaskStatus.TODO:
                continue

            # Verifica se todas as dependências foram completadas
            dependencies_met = True
            for dep_id in task.depends_on:
                dep_task = self.get_task(dep_id)
                if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                    dependencies_met = False
                    break

            if dependencies_met:
                ready_tasks.append(task)

        return ready_tasks

    def update_stats(self) -> None:
        """Atualiza estatísticas da lista"""
        self.completed_tasks = sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED)
        self.failed_tasks = sum(1 for t in self.tasks if t.status == TaskStatus.FAILED)

    def get_progress(self) -> float:
        """Retorna progresso da lista (0-100)"""
        if self.total_tasks == 0:
            return 100.0
        return (self.completed_tasks / self.total_tasks) * 100

    def is_completed(self) -> bool:
        """Verifica se todas as tasks foram completadas"""
        return self.completed_tasks == self.total_tasks

    def has_failures(self) -> bool:
        """Verifica se há tasks que falharam"""
        return self.failed_tasks > 0

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict serializável"""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "tasks": [task.to_dict() for task in self.tasks],
            "sequential_mode": self.sequential_mode,
            "auto_start_next": self.auto_start_next,
            "stop_on_failure": self.stop_on_failure,
            "active": self.active,
            "current_task_id": self.current_task_id,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskList':
        """Cria instância a partir de dict"""
        list_data = data.copy()

        # Converte datetime
        list_data["created_at"] = datetime.fromisoformat(data["created_at"])

        # Converte tasks
        tasks_data = list_data.pop("tasks", [])
        task_list = cls(**list_data)
        task_list.tasks = [Task.from_dict(task_data) for task_data in tasks_data]

        return task_list


class TaskManager:
    """Gerenciador de TODO lists com fluxo sequencial garantido"""

    def __init__(self, storage_dir: str = "./tasks"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)

        self._active_lists: Dict[str, TaskList] = {}
        self._execution_locks: Dict[str, asyncio.Lock] = {}

        # Callback para execução de tasks personalizadas
        self._task_executor = None

    def set_task_executor(self, executor):
        """Define executor customizado para tasks"""
        self._task_executor = executor

    async def create_task_list(self, title: str, description: str = "",
                              sequential: bool = True, auto_start: bool = True) -> TaskList:
        """Cria nova lista de tasks"""
        list_id = str(uuid.uuid4())[:8]

        task_list = TaskList(
            id=list_id,
            title=title,
            description=description,
            sequential_mode=sequential,
            auto_start_next=auto_start
        )

        await self._save_task_list(task_list)
        return task_list

    async def add_task_to_list(self, list_id: str, title: str, description: str = "",
                              depends_on: Optional[List[str]] = None,
                              priority: TaskPriority = TaskPriority.MEDIUM,
                              estimated_duration: Optional[timedelta] = None) -> Task:
        """Adiciona task a uma lista"""
        task_list = await self.load_task_list(list_id)
        if not task_list:
            raise DEILEError(f"Task list {list_id} not found")

        task_id = str(uuid.uuid4())[:8]
        task = Task(
            id=task_id,
            title=title,
            description=description,
            depends_on=depends_on or [],
            priority=priority,
            estimated_duration=estimated_duration
        )

        task_list.add_task(task)
        await self._save_task_list(task_list)

        logger.info(f"Added task {task_id} to list {list_id}")
        return task

    async def start_execution(self, list_id: str) -> Dict[str, Any]:
        """Inicia execução de uma lista de tasks"""
        task_list = await self.load_task_list(list_id)
        if not task_list:
            raise DEILEError(f"Task list {list_id} not found")

        if task_list.active:
            raise DEILEError(f"Task list {list_id} is already active")

        # Ativa lista
        task_list.active = True
        self._active_lists[list_id] = task_list
        self._execution_locks[list_id] = asyncio.Lock()

        await self._save_task_list(task_list)

        logger.info(f"Started execution of task list {list_id}")

        # Inicia execução em background
        asyncio.create_task(self._execute_task_list(list_id))

        return {
            "list_id": list_id,
            "status": "started",
            "total_tasks": task_list.total_tasks
        }

    async def execute_next_task(self, list_id: str) -> Optional[Dict[str, Any]]:
        """Executa próxima task disponível manualmente"""
        async with self._execution_locks.get(list_id, asyncio.Lock()):
            task_list = self._active_lists.get(list_id)
            if not task_list:
                task_list = await self.load_task_list(list_id)
                if not task_list:
                    return None

            # Obtém próxima task
            ready_tasks = task_list.get_next_tasks()
            if not ready_tasks:
                return {
                    "status": "no_tasks_ready",
                    "message": "No tasks ready for execution"
                }

            # Executa primeira task da lista
            task = ready_tasks[0]
            result = await self._execute_task(task, task_list)

            # Salva alterações
            await self._save_task_list(task_list)

            return result

    async def mark_task_completed(self, list_id: str, task_id: str,
                                success: bool = True, result_data: Optional[Dict] = None,
                                error_message: Optional[str] = None) -> bool:
        """Marca task como completada"""
        task_list = self._active_lists.get(list_id)
        if not task_list:
            task_list = await self.load_task_list(list_id)
            if not task_list:
                return False

        task = task_list.get_task(task_id)
        if not task:
            return False

        # Atualiza task
        task.status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
        task.completed_at = datetime.now()
        task.success = success
        task.result_data = result_data
        task.error_message = error_message

        # Atualiza estatísticas
        task_list.update_stats()

        # Se estava em progresso, limpa current_task_id
        if task_list.current_task_id == task_id:
            task_list.current_task_id = None

        await self._save_task_list(task_list)

        logger.info(f"Marked task {task_id} as {'completed' if success else 'failed'}")

        # Se modo automático, tenta executar próxima task
        if task_list.auto_start_next and success:
            asyncio.create_task(self._try_execute_next(list_id))

        return True

    async def get_task_list_status(self, list_id: str) -> Optional[Dict[str, Any]]:
        """Obtém status de uma lista de tasks"""
        task_list = self._active_lists.get(list_id)
        if not task_list:
            task_list = await self.load_task_list(list_id)
            if not task_list:
                return None

        return {
            "id": task_list.id,
            "title": task_list.title,
            "active": task_list.active,
            "progress": task_list.get_progress(),
            "total_tasks": task_list.total_tasks,
            "completed_tasks": task_list.completed_tasks,
            "failed_tasks": task_list.failed_tasks,
            "current_task": task_list.current_task_id,
            "is_completed": task_list.is_completed(),
            "has_failures": task_list.has_failures(),
            "next_tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "priority": task.priority.value
                }
                for task in task_list.get_next_tasks()
            ]
        }

    async def list_all_task_lists(self) -> List[Dict[str, Any]]:
        """Lista todas as listas de tasks"""
        lists_info = []

        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                lists_info.append({
                    "id": data["id"],
                    "title": data["title"],
                    "description": data["description"],
                    "active": data["active"],
                    "total_tasks": data["total_tasks"],
                    "completed_tasks": data["completed_tasks"],
                    "failed_tasks": data["failed_tasks"],
                    "created_at": data["created_at"]
                })

            except Exception as e:
                logger.warning(f"Failed to read task list {file_path}: {e}")

        # Ordena por data de criação
        lists_info.sort(key=lambda x: x["created_at"], reverse=True)
        return lists_info

    async def load_task_list(self, list_id: str) -> Optional[TaskList]:
        """Carrega lista de tasks do disco"""
        file_path = self.storage_dir / f"{list_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return TaskList.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load task list {list_id}: {e}")
            return None

    async def _execute_task_list(self, list_id: str) -> None:
        """Executa lista de tasks automaticamente"""
        while True:
            async with self._execution_locks[list_id]:
                task_list = self._active_lists[list_id]

                # Verifica se lista ainda está ativa
                if not task_list.active:
                    break

                # Verifica se completou ou falhou
                if task_list.is_completed():
                    task_list.active = False
                    await self._save_task_list(task_list)
                    logger.info(f"Task list {list_id} completed successfully")
                    break

                if task_list.has_failures() and task_list.stop_on_failure:
                    task_list.active = False
                    await self._save_task_list(task_list)
                    logger.error(f"Task list {list_id} stopped due to failure")
                    break

                # Obtém próximas tasks
                ready_tasks = task_list.get_next_tasks()
                if not ready_tasks:
                    # Aguarda um pouco e verifica novamente
                    await asyncio.sleep(1)
                    continue

                # Executa primeira task
                task = ready_tasks[0]
                await self._execute_task(task, task_list)
                await self._save_task_list(task_list)

            # Pequena pausa entre iterações
            await asyncio.sleep(0.1)

    async def _execute_task(self, task: Task, task_list: TaskList) -> Dict[str, Any]:
        """Executa uma task individual"""
        # Marca como em progresso
        task.status = TaskStatus.IN_PROGRESS
        task.started_at = datetime.now()
        task_list.current_task_id = task.id

        logger.info(f"Starting execution of task {task.id}: {task.title}")

        try:
            # Se há executor customizado, usa ele
            if self._task_executor:
                result = await self._task_executor.execute_task(task)

                # Atualiza task com resultado
                task.status = TaskStatus.COMPLETED if result.get("success", True) else TaskStatus.FAILED
                task.completed_at = datetime.now()
                task.success = result.get("success", True)
                task.result_data = result.get("data")
                task.error_message = result.get("error")

            else:
                # Execução default (simula execução bem-sucedida)
                await asyncio.sleep(0.5)  # Simula trabalho

                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now()
                task.success = True

            # Atualiza estatísticas
            task_list.update_stats()
            task_list.current_task_id = None

            logger.info(f"Completed task {task.id}: {task.status.value}")

            return {
                "task_id": task.id,
                "status": task.status.value,
                "success": task.success,
                "duration": (task.completed_at - task.started_at).total_seconds() if task.completed_at and task.started_at else 0
            }

        except Exception as e:
            # Marca como falhou
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now()
            task.success = False
            task.error_message = str(e)

            task_list.update_stats()
            task_list.current_task_id = None

            logger.error(f"Failed to execute task {task.id}: {e}")

            return {
                "task_id": task.id,
                "status": task.status.value,
                "success": False,
                "error": str(e)
            }

    async def _try_execute_next(self, list_id: str) -> None:
        """Tenta executar próxima task se disponível"""
        try:
            await self.execute_next_task(list_id)
        except Exception as e:
            logger.error(f"Failed to execute next task in list {list_id}: {e}")

    async def _save_task_list(self, task_list: TaskList) -> None:
        """Salva lista de tasks no disco"""
        file_path = self.storage_dir / f"{task_list.id}.json"

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(task_list.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save task list {task_list.id}: {e}")
            raise


# Singleton instance
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    """Retorna instância singleton do TaskManager"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager