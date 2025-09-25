"""SQLite Task Manager - Sistema robusto de TODO lists com persistência SQLite"""

import sqlite3
import json
import asyncio
import uuid
import logging
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timedelta
from pathlib import Path
import aiosqlite

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
    """Representa uma task individual com validação robusta"""
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

    def __post_init__(self):
        """Validação após inicialização"""
        if not self.id:
            raise ValueError("Task ID cannot be empty")
        if not self.title:
            raise ValueError("Task title cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict serializável com validação"""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "depends_on": json.dumps(self.depends_on),
            "blocks": json.dumps(self.blocks),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "estimated_duration": self.estimated_duration.total_seconds() if self.estimated_duration else None,
            "tags": json.dumps(self.tags),
            "metadata": json.dumps(self.metadata),
            "success": self.success,
            "result_data": json.dumps(self.result_data) if self.result_data else None,
            "error_message": self.error_message
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        """Cria instância a partir de dict com validação"""
        try:
            task_data = data.copy()

            # Remove campos que são específicos do banco mas não da classe Task
            task_data.pop("list_id", None)
            task_data.pop("updated_at", None)

            # Converte enums com validação
            task_data["status"] = TaskStatus(data["status"])
            task_data["priority"] = TaskPriority(data["priority"])

            # Converte JSON fields
            task_data["depends_on"] = json.loads(data.get("depends_on", "[]"))
            task_data["blocks"] = json.loads(data.get("blocks", "[]"))
            task_data["tags"] = json.loads(data.get("tags", "[]"))
            task_data["metadata"] = json.loads(data.get("metadata", "{}"))

            if data.get("result_data"):
                task_data["result_data"] = json.loads(data["result_data"])

            # Converte datetime com validação
            task_data["created_at"] = datetime.fromisoformat(data["created_at"])
            if data.get("started_at"):
                task_data["started_at"] = datetime.fromisoformat(data["started_at"])
            if data.get("completed_at"):
                task_data["completed_at"] = datetime.fromisoformat(data["completed_at"])
            if data.get("estimated_duration"):
                task_data["estimated_duration"] = timedelta(seconds=data["estimated_duration"])

            return cls(**task_data)
        except Exception as e:
            raise ValueError(f"Failed to create Task from data: {e}")


@dataclass
class TaskList:
    """Representa uma lista de tasks com fluxo sequencial"""
    id: str
    title: str
    description: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    # Configurações de execução
    sequential_mode: bool = True
    auto_start_next: bool = True
    stop_on_failure: bool = True

    # Status da lista
    active: bool = False
    current_task_id: Optional[str] = None

    # Estatísticas (será calculado dinamicamente)
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0

    def __post_init__(self):
        """Validação após inicialização"""
        if not self.id:
            raise ValueError("TaskList ID cannot be empty")
        if not self.title:
            raise ValueError("TaskList title cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict serializável"""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
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
        list_data["created_at"] = datetime.fromisoformat(data["created_at"])

        # Remove campos que não fazem parte do __init__
        list_data.pop("updated_at", None)

        return cls(**list_data)


class SQLiteTaskManager:
    """Gerenciador de TODO lists com persistência SQLite robusta"""

    def __init__(self, db_path: str = "./deile_tasks.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)

        # Cache em memória para performance
        self._cache: Dict[str, TaskList] = {}
        self._task_cache: Dict[str, List[Task]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_ttl = 300  # 5 minutos

        # Lock para operações assíncronas
        self._db_lock = asyncio.Lock()

        # Inicializa schema
        asyncio.create_task(self._initialize_database())

    async def _initialize_database(self):
        """Inicializa schema do banco de dados"""
        async with aiosqlite.connect(self.db_path) as db:
            # Tabela de task lists
            await db.execute("""
                CREATE TABLE IF NOT EXISTS task_lists (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    sequential_mode BOOLEAN DEFAULT TRUE,
                    auto_start_next BOOLEAN DEFAULT TRUE,
                    stop_on_failure BOOLEAN DEFAULT TRUE,
                    active BOOLEAN DEFAULT FALSE,
                    current_task_id TEXT,
                    total_tasks INTEGER DEFAULT 0,
                    completed_tasks INTEGER DEFAULT 0,
                    failed_tasks INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Tabela de tasks
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    list_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    depends_on TEXT,  -- JSON array
                    blocks TEXT,      -- JSON array
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    estimated_duration REAL,
                    tags TEXT,        -- JSON array
                    metadata TEXT,    -- JSON object
                    success BOOLEAN,
                    result_data TEXT, -- JSON object
                    error_message TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (list_id) REFERENCES task_lists (id) ON DELETE CASCADE
                )
            """)

            # Índices para performance
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_list_id ON tasks(list_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")

            await db.commit()
            logger.info(f"SQLite database initialized at {self.db_path}")

    async def create_task_list(self, title: str, description: str = "",
                              sequential: bool = True, auto_start: bool = True) -> TaskList:
        """Cria nova lista de tasks com persistência SQLite"""
        list_id = str(uuid.uuid4())[:8]

        task_list = TaskList(
            id=list_id,
            title=title,
            description=description,
            sequential_mode=sequential,
            auto_start_next=auto_start
        )

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO task_lists
                    (id, title, description, created_at, sequential_mode, auto_start_next, stop_on_failure, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_list.id, task_list.title, task_list.description,
                    task_list.created_at.isoformat(), task_list.sequential_mode,
                    task_list.auto_start_next, task_list.stop_on_failure, task_list.active
                ))
                await db.commit()

        # Atualiza cache
        self._cache[list_id] = task_list
        self._task_cache[list_id] = []
        self._cache_timestamps[list_id] = asyncio.get_event_loop().time()

        logger.info(f"Created task list {list_id}: {title}")
        return task_list

    async def add_task_to_list(self, list_id: str, title: str, description: str = "",
                              depends_on: Optional[List[str]] = None,
                              priority: TaskPriority = TaskPriority.MEDIUM,
                              estimated_duration: Optional[timedelta] = None) -> Task:
        """Adiciona task a uma lista com validação de integridade"""

        # Verifica se lista existe
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

        # Validação de dependências (só valida se as dependências não estão vazias e existem)
        if depends_on:
            try:
                existing_tasks = await self._get_tasks_for_list(list_id)
                existing_task_ids = {t.id for t in existing_tasks}
                invalid_deps = set(depends_on) - existing_task_ids
                if invalid_deps:
                    logger.warning(f"Some dependencies not found yet: {invalid_deps}. Will be validated later.")
                    # Em vez de falhar, apenas avisa - permite dependências futuras
            except Exception as e:
                logger.warning(f"Could not validate dependencies: {e}")

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                # Insere task
                task_dict = task.to_dict()
                await db.execute("""
                    INSERT INTO tasks
                    (id, list_id, title, description, status, priority, depends_on, blocks,
                     created_at, started_at, completed_at, estimated_duration, tags, metadata,
                     success, result_data, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task.id, list_id, task.title, task.description, task.status.value,
                    task.priority.value, task_dict["depends_on"], task_dict["blocks"],
                    task_dict["created_at"], task_dict["started_at"], task_dict["completed_at"],
                    task_dict["estimated_duration"], task_dict["tags"], task_dict["metadata"],
                    task.success, task_dict["result_data"], task.error_message
                ))

                # Atualiza contadores da lista
                await db.execute("""
                    UPDATE task_lists
                    SET total_tasks = (SELECT COUNT(*) FROM tasks WHERE list_id = ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (list_id, list_id))

                await db.commit()

        # Limpa cache para forçar reload
        self._invalidate_cache(list_id)

        logger.info(f"Added task {task_id} to list {list_id}")
        return task

    async def load_task_list(self, list_id: str) -> Optional[TaskList]:
        """Carrega lista de tasks do SQLite com cache"""

        # Verifica cache primeiro
        if list_id in self._cache and self._is_cache_valid(list_id):
            return self._cache[list_id]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM task_lists WHERE id = ?", (list_id,)) as cursor:
                row = await cursor.fetchone()

                if not row:
                    return None

                # Converte para TaskList
                data = dict(row)
                task_list = TaskList.from_dict(data)

                # Recalcula estatísticas
                await self._update_task_list_stats(task_list, db)

                # Atualiza cache
                self._cache[list_id] = task_list
                self._cache_timestamps[list_id] = asyncio.get_event_loop().time()

                return task_list

    async def _get_tasks_for_list(self, list_id: str) -> List[Task]:
        """Obtém todas as tasks de uma lista"""

        # Verifica cache primeiro
        if list_id in self._task_cache and self._is_cache_valid(list_id):
            return self._task_cache[list_id]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM tasks WHERE list_id = ? ORDER BY created_at ASC
            """, (list_id,)) as cursor:
                rows = await cursor.fetchall()

                tasks = []
                for row in rows:
                    try:
                        task = Task.from_dict(dict(row))
                        tasks.append(task)
                    except Exception as e:
                        logger.error(f"Failed to load task {row['id']}: {e}")

                # Atualiza cache
                self._task_cache[list_id] = tasks
                return tasks

    async def get_next_tasks(self, list_id: str) -> List[Task]:
        """Obtém próximas tasks prontas para execução"""
        tasks = await self._get_tasks_for_list(list_id)
        task_list = await self.load_task_list(list_id)

        if not task_list or not task_list.sequential_mode:
            return [task for task in tasks if task.status == TaskStatus.TODO]

        # Modo sequencial: verifica dependências
        ready_tasks = []
        for task in tasks:
            if task.status != TaskStatus.TODO:
                continue

            # Verifica se todas as dependências foram completadas
            dependencies_met = True
            for dep_id in task.depends_on:
                dep_task = next((t for t in tasks if t.id == dep_id), None)
                if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                    dependencies_met = False
                    break

            if dependencies_met:
                ready_tasks.append(task)

        return ready_tasks

    async def mark_task_completed(self, list_id: str, task_id: str,
                                success: bool = True, result_data: Optional[Dict] = None,
                                error_message: Optional[str] = None) -> bool:
        """Marca task como completada com transação atômica"""

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                # Verifica se task existe
                async with db.execute("SELECT id FROM tasks WHERE id = ? AND list_id = ?",
                                    (task_id, list_id)) as cursor:
                    if not await cursor.fetchone():
                        return False

                # Atualiza task
                status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
                now = datetime.now().isoformat()

                await db.execute("""
                    UPDATE tasks
                    SET status = ?, completed_at = ?, success = ?,
                        result_data = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND list_id = ?
                """, (
                    status.value, now, success,
                    json.dumps(result_data) if result_data else None,
                    error_message, task_id, list_id
                ))

                # Atualiza estatísticas da lista
                await db.execute("""
                    UPDATE task_lists
                    SET completed_tasks = (SELECT COUNT(*) FROM tasks WHERE list_id = ? AND status = 'completed'),
                        failed_tasks = (SELECT COUNT(*) FROM tasks WHERE list_id = ? AND status = 'failed'),
                        current_task_id = CASE WHEN current_task_id = ? THEN NULL ELSE current_task_id END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (list_id, list_id, task_id, list_id))

                await db.commit()

        # Limpa cache
        self._invalidate_cache(list_id)

        logger.info(f"Marked task {task_id} as {'completed' if success else 'failed'}")
        return True

    async def get_task_list_status(self, list_id: str) -> Optional[Dict[str, Any]]:
        """Obtém status de uma lista de tasks"""
        task_list = await self.load_task_list(list_id)
        if not task_list:
            return None

        tasks = await self._get_tasks_for_list(list_id)
        next_tasks = await self.get_next_tasks(list_id)

        return {
            "id": task_list.id,
            "title": task_list.title,
            "active": task_list.active,
            "progress": (task_list.completed_tasks / max(1, task_list.total_tasks)) * 100,
            "total_tasks": task_list.total_tasks,
            "completed_tasks": task_list.completed_tasks,
            "failed_tasks": task_list.failed_tasks,
            "current_task": task_list.current_task_id,
            "is_completed": task_list.completed_tasks == task_list.total_tasks,
            "has_failures": task_list.failed_tasks > 0,
            "next_tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "priority": task.priority.value
                }
                for task in next_tasks
            ]
        }

    async def _update_task_list_stats(self, task_list: TaskList, db: aiosqlite.Connection):
        """Atualiza estatísticas da task list"""
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM tasks WHERE list_id = ?
        """, (task_list.id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                task_list.total_tasks = row[0]
                task_list.completed_tasks = row[1] or 0
                task_list.failed_tasks = row[2] or 0

    def _is_cache_valid(self, list_id: str) -> bool:
        """Verifica se cache é válido"""
        if list_id not in self._cache_timestamps:
            return False

        age = asyncio.get_event_loop().time() - self._cache_timestamps[list_id]
        return age < self._cache_ttl

    def _invalidate_cache(self, list_id: str):
        """Invalida cache para uma lista"""
        self._cache.pop(list_id, None)
        self._task_cache.pop(list_id, None)
        self._cache_timestamps.pop(list_id, None)

    async def cleanup_old_tasks(self, days: int = 30):
        """Remove tasks antigas para manter banco limpo"""
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    DELETE FROM task_lists
                    WHERE created_at < ? AND active = FALSE
                """, (cutoff_date,))
                await db.commit()

        logger.info(f"Cleaned up tasks older than {days} days")


# Singleton instance
_sqlite_task_manager: Optional[SQLiteTaskManager] = None


def get_sqlite_task_manager() -> SQLiteTaskManager:
    """Retorna instância singleton do SQLiteTaskManager"""
    global _sqlite_task_manager
    if _sqlite_task_manager is None:
        _sqlite_task_manager = SQLiteTaskManager()
    return _sqlite_task_manager