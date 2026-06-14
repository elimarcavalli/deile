"""Semantic Memory - Conhecimento estruturado e embeddings"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SemanticMemory:
    """Gerencia conhecimento estruturado (implementação básica)"""

    def __init__(self, storage_dir: Path, enable_vector_store: bool = True,
                 vector_dimensions: int = 768, similarity_threshold: float = 0.7):
        self.storage_dir = storage_dir
        self.enable_vector_store = enable_vector_store
        self.vector_dimensions = vector_dimensions
        self.similarity_threshold = similarity_threshold

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_file = self.storage_dir / "knowledge.jsonl"

        self._knowledge_base = []
        self._is_initialized = False

    async def initialize(self) -> None:
        """Inicialização (load offloaded to a worker thread)."""
        if self._is_initialized:
            return

        # Carrega conhecimento existente.
        if self.knowledge_file.exists():
            self._knowledge_base.extend(
                await asyncio.to_thread(_read_jsonl, self.knowledge_file)
            )

        self._is_initialized = True
        logger.info("SemanticMemory inicializada")

    async def store_knowledge(self, knowledge: Dict[str, Any]) -> None:
        """Armazena conhecimento.

        Copia o dict antes de adicionar campos internos — caso contrário o
        argumento do caller é mutado (``stored_at`` aparece magicamente em
        dicts que continuam sendo iterados/serializados rio acima).

        Sync ``open()`` é delegado para uma worker thread porque este método
        é chamado a cada interação (via ``MemoryManager`` background task);
        bloquear o loop nesse caminho viola o princípio 03 §1.
        """
        record = dict(knowledge)
        record['stored_at'] = record.get('extracted_at', record.get('stored_at', 0))
        self._knowledge_base.append(record)

        await asyncio.to_thread(_append_jsonl_record, self.knowledge_file, record)

    async def search_knowledge(self, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """Busca conhecimento (implementação básica)"""
        results = []
        query_lower = query.lower()

        for knowledge in self._knowledge_base[-max_results:]:  # Pega os mais recentes
            content = str(knowledge.get('user_input', '')) + str(knowledge.get('agent_response', ''))
            if query_lower in content.lower():
                results.append({
                    "content": content,
                    "score": 1.0,
                    "metadata": knowledge
                })

        return results

    async def store_correction(self, interaction_id: str, correction_data: Dict[str, Any]) -> None:
        """Armazena correção"""
        correction = {
            "type": "correction",
            "interaction_id": interaction_id,
            "correction_data": correction_data,
            "stored_at": asyncio.get_event_loop().time()
        }
        await self.store_knowledge(correction)

    async def get_stats(self) -> Dict[str, Any]:
        """Estatísticas"""
        return {
            "total_knowledge_entries": len(self._knowledge_base),
            "memory_mb": 0.1,
            "is_initialized": self._is_initialized
        }

    async def shutdown(self) -> None:
        """Finalização"""
        self._is_initialized = False

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Sync JSONL reader called from ``asyncio.to_thread``."""
    out: List[Dict[str, Any]] = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _append_jsonl_record(path: Path, record: Dict[str, Any]) -> None:
    """Sync JSONL append called from ``asyncio.to_thread``."""
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
