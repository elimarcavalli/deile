"""Working Memory - Cache de curto prazo e contexto ativo"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field
from collections import OrderedDict
import json
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class WorkingMemoryEntry:
    """Entrada da working memory"""
    entry_id: str
    content: str
    entry_type: str  # 'interaction', 'context', 'temp_data'
    timestamp: float
    ttl: float  # Time to live
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: Set[str] = field(default_factory=set)

    @property
    def is_expired(self) -> bool:
        """Verifica se a entrada expirou"""
        return time.time() > (self.timestamp + self.ttl)

    @property
    def age_seconds(self) -> float:
        """Idade da entrada em segundos"""
        return time.time() - self.timestamp

    def access(self) -> None:
        """Registra acesso à entrada"""
        self.access_count += 1
        self.last_accessed = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """Serializa entrada para dicionário"""
        return {
            "entry_id": self.entry_id,
            "content": self.content,
            "entry_type": self.entry_type,
            "timestamp": self.timestamp,
            "ttl": self.ttl,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "metadata": self.metadata,
            "tags": list(self.tags)
        }


class WorkingMemory:
    """Working Memory - Gerencia contexto ativo e cache de curto prazo

    Características:
    - Cache LRU com TTL para entradas
    - Busca por conteúdo e tags
    - Priorização baseada em frequência de acesso
    - Limpeza automática de entradas expiradas
    """

    def __init__(self, max_size: int = 8000, ttl: int = 3600):
        self.max_size = max_size  # Máximo de caracteres total
        self.default_ttl = ttl    # TTL padrão em segundos

        # Storage ordenado (LRU)
        self._entries: OrderedDict[str, WorkingMemoryEntry] = OrderedDict()
        self._current_size = 0

        # Índices para busca rápida
        self._type_index: Dict[str, Set[str]] = {}  # type -> set of entry_ids
        self._tag_index: Dict[str, Set[str]] = {}   # tag -> set of entry_ids

        # Task de limpeza automática
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_initialized = False

        # Estatísticas
        self._stats = {
            "entries_created": 0,
            "entries_accessed": 0,
            "entries_evicted": 0,
            "entries_expired": 0,
            "searches_performed": 0
        }

        logger.debug("WorkingMemory inicializada")

    async def initialize(self) -> None:
        """Inicializa a working memory"""
        if self._is_initialized:
            return

        # Inicia task de limpeza automática (executa a cada 5 minutos)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._is_initialized = True

        logger.info("WorkingMemory inicializada com sucesso")

    async def store(
        self,
        content: str,
        entry_type: str = "context",
        ttl: Optional[int] = None,
        tags: Set[str] = None,
        metadata: Dict[str, Any] = None
    ) -> str:
        """Armazena conteúdo na working memory

        Args:
            content: Conteúdo a ser armazenado
            entry_type: Tipo da entrada ('interaction', 'context', 'temp_data')
            ttl: Time to live em segundos (usa padrão se None)
            tags: Tags para indexação
            metadata: Metadata adicional

        Returns:
            str: ID da entrada criada
        """
        # Gera ID baseado no conteúdo e timestamp
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        entry_id = f"{entry_type}_{content_hash}_{int(time.time())}"

        # Cria entrada
        entry = WorkingMemoryEntry(
            entry_id=entry_id,
            content=content,
            entry_type=entry_type,
            timestamp=time.time(),
            ttl=ttl or self.default_ttl,
            metadata=metadata or {},
            tags=tags or set()
        )

        # Verifica se há espaço
        await self._ensure_space(len(content))

        # Adiciona à storage
        self._entries[entry_id] = entry
        self._current_size += len(content)

        # Atualiza índices
        self._update_indices(entry)

        self._stats["entries_created"] += 1
        logger.debug(f"Entrada armazenada na working memory: {entry_id} ({len(content)} chars)")

        return entry_id

    async def store_interaction(
        self,
        user_input: str,
        agent_response: str,
        context: Dict[str, Any] = None
    ) -> str:
        """Armazena uma interação completa

        Args:
            user_input: Input do usuário
            agent_response: Resposta do agente
            context: Contexto adicional

        Returns:
            str: ID da entrada da interação
        """
        interaction_content = json.dumps({
            "user_input": user_input,
            "agent_response": agent_response,
            "context": context or {}
        }, ensure_ascii=False)

        return await self.store(
            content=interaction_content,
            entry_type="interaction",
            tags={"user_interaction", "recent"},
            metadata={
                "user_input_length": len(user_input),
                "agent_response_length": len(agent_response),
                "interaction_timestamp": time.time()
            }
        )

    async def retrieve(self, entry_id: str) -> Optional[WorkingMemoryEntry]:
        """Recupera entrada específica pelo ID"""
        if entry_id not in self._entries:
            return None

        entry = self._entries[entry_id]

        # Verifica se expirou
        if entry.is_expired:
            await self._remove_entry(entry_id)
            return None

        # Registra acesso e move para final (LRU)
        entry.access()
        self._entries.move_to_end(entry_id)
        self._stats["entries_accessed"] += 1

        return entry

    async def search(
        self,
        query: str,
        max_results: int = 10,
        entry_type: str = None,
        tags: Set[str] = None
    ) -> List[Dict[str, Any]]:
        """Busca entradas na working memory

        Args:
            query: Query de busca (busca no conteúdo)
            max_results: Número máximo de resultados
            entry_type: Filtro por tipo de entrada
            tags: Filtro por tags

        Returns:
            Lista de entradas encontradas
        """
        self._stats["searches_performed"] += 1
        results = []

        # Remove entradas expiradas primeiro
        await self._cleanup_expired()

        # Filtra candidatos por tipo e tags se especificado
        candidates = set(self._entries.keys())

        if entry_type and entry_type in self._type_index:
            candidates &= self._type_index[entry_type]

        if tags:
            for tag in tags:
                if tag in self._tag_index:
                    candidates &= self._tag_index[tag]

        # Busca textual nos candidatos
        query_lower = query.lower()
        scored_results = []

        for entry_id in candidates:
            if entry_id not in self._entries:
                continue

            entry = self._entries[entry_id]

            # Calcula score baseado em:
            # - Relevância textual
            # - Frequência de acesso
            # - Recência
            content_lower = entry.content.lower()
            text_score = 0

            # Score de texto
            if query_lower in content_lower:
                text_score = content_lower.count(query_lower) * 10
                if content_lower.startswith(query_lower):
                    text_score += 5

            if text_score == 0:
                continue  # Não há match textual

            # Score de acesso
            access_score = min(entry.access_count * 2, 20)

            # Score de recência (mais recente = maior score)
            age_hours = entry.age_seconds / 3600
            recency_score = max(0, 10 - age_hours)

            total_score = text_score + access_score + recency_score

            scored_results.append((total_score, entry))

        # Ordena por score e pega os melhores
        scored_results.sort(key=lambda x: x[0], reverse=True)

        for score, entry in scored_results[:max_results]:
            # Registra acesso
            entry.access()

            results.append({
                "entry_id": entry.entry_id,
                "content": entry.content,
                "entry_type": entry.entry_type,
                "score": score,
                "age_seconds": entry.age_seconds,
                "access_count": entry.access_count,
                "tags": list(entry.tags),
                "metadata": entry.metadata
            })

        logger.debug(f"Busca na working memory: '{query}' -> {len(results)} resultados")
        return results

    async def update_with_feedback(
        self,
        entry_id: str,
        feedback_type: str,
        feedback_data: Dict[str, Any]
    ) -> bool:
        """Atualiza entrada com feedback

        Args:
            entry_id: ID da entrada
            feedback_type: Tipo do feedback
            feedback_data: Dados do feedback

        Returns:
            bool: True se atualização foi bem-sucedida
        """
        entry = await self.retrieve(entry_id)
        if not entry:
            return False

        # Adiciona feedback ao metadata
        if "feedback" not in entry.metadata:
            entry.metadata["feedback"] = []

        entry.metadata["feedback"].append({
            "type": feedback_type,
            "data": feedback_data,
            "timestamp": time.time()
        })

        # Ajusta TTL baseado no feedback
        if feedback_type == "positive":
            # Feedback positivo aumenta TTL
            entry.ttl *= 1.5
            entry.tags.add("positive_feedback")
        elif feedback_type == "negative":
            # Feedback negativo diminui TTL
            entry.ttl *= 0.5
            entry.tags.add("negative_feedback")

        logger.debug(f"Feedback aplicado à entrada {entry_id}: {feedback_type}")
        return True

    async def clear_type(self, entry_type: str) -> int:
        """Remove todas as entradas de um tipo específico

        Args:
            entry_type: Tipo das entradas a remover

        Returns:
            int: Número de entradas removidas
        """
        if entry_type not in self._type_index:
            return 0

        entry_ids_to_remove = list(self._type_index[entry_type])
        removed_count = 0

        for entry_id in entry_ids_to_remove:
            if await self._remove_entry(entry_id):
                removed_count += 1

        logger.info(f"Removidas {removed_count} entradas do tipo '{entry_type}'")
        return removed_count

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas da working memory"""
        # Remove expiradas para estatística precisa
        await self._cleanup_expired()

        type_counts = {}
        for entry_type, entry_ids in self._type_index.items():
            type_counts[entry_type] = len(entry_ids)

        return {
            "total_entries": len(self._entries),
            "current_size_chars": self._current_size,
            "max_size_chars": self.max_size,
            "memory_usage_percent": (self._current_size / self.max_size) * 100,
            "memory_mb": self._current_size * 2 / (1024 * 1024),  # Estimativa rough
            "entries_by_type": type_counts,
            "total_tags": len(self._tag_index),
            "stats": self._stats.copy(),
            "is_initialized": self._is_initialized
        }

    async def _ensure_space(self, needed_space: int) -> None:
        """Garante espaço suficiente removendo entradas antigas"""
        while self._current_size + needed_space > self.max_size and self._entries:
            # Remove entrada mais antiga (LRU)
            oldest_id = next(iter(self._entries))
            await self._remove_entry(oldest_id)
            self._stats["entries_evicted"] += 1

    async def _remove_entry(self, entry_id: str) -> bool:
        """Remove entrada específica"""
        if entry_id not in self._entries:
            return False

        entry = self._entries[entry_id]

        # Remove da storage principal
        del self._entries[entry_id]
        self._current_size -= len(entry.content)

        # Remove dos índices
        self._remove_from_indices(entry)

        return True

    def _update_indices(self, entry: WorkingMemoryEntry) -> None:
        """Atualiza índices com nova entrada"""
        # Índice por tipo
        if entry.entry_type not in self._type_index:
            self._type_index[entry.entry_type] = set()
        self._type_index[entry.entry_type].add(entry.entry_id)

        # Índice por tags
        for tag in entry.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = set()
            self._tag_index[tag].add(entry.entry_id)

    def _remove_from_indices(self, entry: WorkingMemoryEntry) -> None:
        """Remove entrada dos índices"""
        # Remove do índice de tipo
        if entry.entry_type in self._type_index:
            self._type_index[entry.entry_type].discard(entry.entry_id)
            if not self._type_index[entry.entry_type]:
                del self._type_index[entry.entry_type]

        # Remove do índice de tags
        for tag in entry.tags:
            if tag in self._tag_index:
                self._tag_index[tag].discard(entry.entry_id)
                if not self._tag_index[tag]:
                    del self._tag_index[tag]

    async def _cleanup_expired(self) -> int:
        """Remove entradas expiradas"""
        expired_ids = []

        for entry_id, entry in self._entries.items():
            if entry.is_expired:
                expired_ids.append(entry_id)

        removed_count = 0
        for entry_id in expired_ids:
            if await self._remove_entry(entry_id):
                removed_count += 1

        if removed_count > 0:
            self._stats["entries_expired"] += removed_count
            logger.debug(f"Removidas {removed_count} entradas expiradas da working memory")

        return removed_count

    async def _cleanup_loop(self) -> None:
        """Loop de limpeza automática executado em background"""
        while True:
            try:
                await asyncio.sleep(300)  # 5 minutos
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erro no loop de limpeza da working memory: {e}")

    async def shutdown(self) -> None:
        """Finaliza a working memory"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Limpa dados
        self._entries.clear()
        self._type_index.clear()
        self._tag_index.clear()
        self._current_size = 0
        self._is_initialized = False

        logger.info("WorkingMemory finalizada")