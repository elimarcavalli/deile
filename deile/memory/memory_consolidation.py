"""Memory Consolidation - Otimização e limpeza automática de memória"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any

from .working_memory import WorkingMemory
from .episodic_memory import EpisodicMemory
from .semantic_memory import SemanticMemory
from .procedural_memory import ProceduralMemory

logger = logging.getLogger(__name__)


class MemoryConsolidator:
    """Consolida e otimiza diferentes tipos de memória"""

    def __init__(self, working_memory: WorkingMemory, episodic_memory: EpisodicMemory,
                 semantic_memory: SemanticMemory, procedural_memory: ProceduralMemory):
        self.working_memory = working_memory
        self.episodic_memory = episodic_memory
        self.semantic_memory = semantic_memory
        self.procedural_memory = procedural_memory

    async def consolidate_all(self, force: bool = False) -> Dict[str, Any]:
        """Consolida todos os tipos de memória"""
        start_time = time.time()
        report = {
            "consolidation_started": start_time,
            "working_memory": {},
            "episodic_memory": {},
            "semantic_memory": {},
            "procedural_memory": {},
            "total_time": 0
        }

        try:
            # Consolida working memory
            working_stats = await self.working_memory.get_stats()
            cleaned_entries = await self.working_memory._cleanup_expired()
            report["working_memory"] = {
                "entries_before": working_stats.get("total_entries", 0),
                "expired_cleaned": cleaned_entries
            }

            # Outras consolidações seriam implementadas aqui
            report["total_time"] = time.time() - start_time

            logger.info(f"Consolidação completa em {report['total_time']:.2f}s")
            return report

        except Exception as e:
            logger.error(f"Erro na consolidação: {e}")
            report["error"] = str(e)
            return report