"""Sistema híbrido de memória enterprise-grade para DEILE 2.0 ULTRA

Implementa arquitetura de memória multi-camadas com:
- Working Memory: Contexto ativo e cache de curto prazo
- Episodic Memory: Histórico de sessões e conversas
- Semantic Memory: Conhecimento estruturado com vector DB
- Procedural Memory: Patterns aprendidos e habilidades adquiridas
- Memory Consolidation: Otimização e limpeza automática
"""

from .memory_manager import MemoryManager
from .working_memory import WorkingMemory
from .episodic_memory import EpisodicMemory
from .semantic_memory import SemanticMemory
from .procedural_memory import ProceduralMemory
from .memory_consolidation import MemoryConsolidator

__all__ = [
    "MemoryManager",
    "WorkingMemory",
    "EpisodicMemory",
    "SemanticMemory",
    "ProceduralMemory",
    "MemoryConsolidator"
]

__version__ = "2.0.0"