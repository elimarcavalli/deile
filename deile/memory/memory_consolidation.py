"""Memory Consolidation - Otimização e limpeza automática de memória"""

import logging
import time
from typing import Any, Dict

from .episodic_memory import EpisodicMemory
from .procedural_memory import ProceduralMemory
from .semantic_memory import SemanticMemory
from .working_memory import WorkingMemory

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
            # Consolida working memory.
            #
            # Antes da correção, get_stats() era chamado primeiro — e
            # get_stats() internamente já faz _cleanup_expired(), então a
            # segunda chamada nunca tinha nada para limpar (expired_cleaned
            # sempre = 0) e entries_before reportava o estado JÁ pós-limpeza.
            # Capturamos a contagem ANTES de qualquer operação de limpeza.
            entries_before = len(self.working_memory._entries)
            cleaned_entries = await self.working_memory._cleanup_expired()
            report["working_memory"] = {
                "entries_before": entries_before,
                "expired_cleaned": cleaned_entries,
                "entries_after": len(self.working_memory._entries),
            }

            # Outras consolidações seriam implementadas aqui
            report["total_time"] = time.time() - start_time

            logger.info(f"Consolidação completa em {report['total_time']:.2f}s")
            return report

        except Exception as e:
            logger.error(f"Erro na consolidação: {e}")
            report["error"] = str(e)
            return report