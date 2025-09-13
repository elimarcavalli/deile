"""Episodic Memory - Histórico de sessões e conversas"""

import asyncio
import logging
import time
import json
from typing import Dict, List, Optional, Any
from pathlib import Path
from dataclasses import dataclass, field
import sqlite3
import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class Episode:
    """Representa um episódio (interação) armazenado"""
    episode_id: str
    session_id: str
    user_input: str
    agent_response: str
    timestamp: float
    context: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


class EpisodicMemory:
    """Gerencia histórico de episódios/interações usando SQLite"""

    def __init__(self, storage_dir: Path, max_episodes_per_session: int = 1000, retention_days: int = 30):
        self.storage_dir = storage_dir
        self.max_episodes_per_session = max_episodes_per_session
        self.retention_days = retention_days

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_dir / "episodes.db"

        self._is_initialized = False

    async def initialize(self) -> None:
        """Inicializa o banco de dados"""
        if self._is_initialized:
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    user_input TEXT,
                    agent_response TEXT,
                    timestamp REAL,
                    context TEXT,
                    metadata TEXT,
                    INDEX(session_id),
                    INDEX(timestamp)
                )
            """)
            await db.commit()

        self._is_initialized = True
        logger.info("EpisodicMemory inicializada")

    async def store_episode(
        self,
        user_input: str,
        agent_response: str,
        context: Dict[str, Any] = None,
        session_id: str = None
    ) -> str:
        """Armazena um novo episódio"""
        episode_id = f"ep_{int(time.time() * 1000)}"
        session_id = session_id or "default"

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO episodes (episode_id, session_id, user_input, agent_response, timestamp, context, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                episode_id,
                session_id,
                user_input,
                agent_response,
                time.time(),
                json.dumps(context or {}),
                json.dumps({})
            ))
            await db.commit()

        return episode_id

    async def search_episodes(
        self,
        query: str,
        session_id: str = None,
        max_results: int = 10
    ) -> List[Dict[str, Any]]:
        """Busca episódios"""
        results = []

        async with aiosqlite.connect(self.db_path) as db:
            if session_id:
                cursor = await db.execute("""
                    SELECT * FROM episodes
                    WHERE session_id = ? AND (user_input LIKE ? OR agent_response LIKE ?)
                    ORDER BY timestamp DESC LIMIT ?
                """, (session_id, f"%{query}%", f"%{query}%", max_results))
            else:
                cursor = await db.execute("""
                    SELECT * FROM episodes
                    WHERE user_input LIKE ? OR agent_response LIKE ?
                    ORDER BY timestamp DESC LIMIT ?
                """, (f"%{query}%", f"%{query}%", max_results))

            rows = await cursor.fetchall()
            for row in rows:
                results.append({
                    "episode_id": row[0],
                    "session_id": row[1],
                    "user_input": row[2],
                    "agent_response": row[3],
                    "timestamp": row[4],
                    "context": json.loads(row[5]),
                    "metadata": json.loads(row[6])
                })

        return results

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM episodes")
            total_episodes = (await cursor.fetchone())[0]

            cursor = await db.execute("SELECT COUNT(DISTINCT session_id) FROM episodes")
            total_sessions = (await cursor.fetchone())[0]

        return {
            "total_episodes": total_episodes,
            "total_sessions": total_sessions,
            "memory_mb": 0.1,  # Estimativa básica
            "is_initialized": self._is_initialized
        }

    async def shutdown(self) -> None:
        """Finalização"""
        self._is_initialized = False