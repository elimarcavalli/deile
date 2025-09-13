"""Procedural Memory - Patterns aprendidos e habilidades adquiridas"""

import asyncio
import logging
import json
from typing import Dict, List, Optional, Any
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)


class ProceduralMemory:
    """Gerencia patterns e habilidades aprendidas"""

    def __init__(self, storage_dir: Path, min_frequency: int = 3, confidence_threshold: float = 0.8):
        self.storage_dir = storage_dir
        self.min_frequency = min_frequency
        self.confidence_threshold = confidence_threshold

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.patterns_file = self.storage_dir / "patterns.json"

        self._patterns = defaultdict(lambda: {"frequency": 0, "confidence": 0.0, "data": {}})
        self._is_initialized = False

    async def initialize(self) -> None:
        """Inicialização"""
        if self._is_initialized:
            return

        if self.patterns_file.exists():
            with open(self.patterns_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._patterns.update(data)

        self._is_initialized = True
        logger.info("ProceduralMemory inicializada")

    async def analyze_interaction(self, pattern_data: Dict[str, Any]) -> None:
        """Analisa padrões de interação"""
        # Gera pattern key baseado nos dados
        pattern_key = f"input_len_{pattern_data.get('input_length', 0) // 100 * 100}"

        self._patterns[pattern_key]["frequency"] += 1
        self._patterns[pattern_key]["data"] = pattern_data

    async def get_relevant_patterns(self, query: str) -> List[Dict[str, Any]]:
        """Obtém patterns relevantes"""
        results = []
        for pattern_key, pattern_info in self._patterns.items():
            if pattern_info["frequency"] >= self.min_frequency:
                results.append({
                    "pattern": pattern_key,
                    "frequency": pattern_info["frequency"],
                    "confidence": pattern_info["confidence"],
                    "data": pattern_info["data"]
                })

        return results[:10]  # Limita resultados

    async def update_pattern_effectiveness(
        self,
        interaction_id: str,
        feedback_type: str,
        feedback_data: Dict[str, Any]
    ) -> None:
        """Atualiza efetividade dos patterns"""
        # Implementação básica - pode ser expandida
        pass

    async def get_stats(self) -> Dict[str, Any]:
        """Estatísticas"""
        return {
            "total_patterns": len(self._patterns),
            "active_patterns": sum(1 for p in self._patterns.values() if p["frequency"] >= self.min_frequency),
            "memory_mb": 0.1,
            "is_initialized": self._is_initialized
        }

    async def shutdown(self) -> None:
        """Finalização - salva patterns"""
        with open(self.patterns_file, 'w', encoding='utf-8') as f:
            json.dump(dict(self._patterns), f, ensure_ascii=False, indent=2)

        self._is_initialized = False