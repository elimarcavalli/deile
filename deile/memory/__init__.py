"""Sistema híbrido de memória enterprise-grade para DEILE 2.0 ULTRA

Implementa arquitetura de memória multi-camadas com:
- Working Memory: Contexto ativo e cache de curto prazo
- Episodic Memory: Histórico de sessões e conversas
- Semantic Memory: Conhecimento estruturado com vector DB
- Procedural Memory: Patterns aprendidos e habilidades adquiridas
- Memory Consolidation: Otimização e limpeza automática
"""

from .episodic_memory import EpisodicMemory
from .memory_consolidation import MemoryConsolidator
from .memory_manager import MemoryManager
from .procedural_memory import ProceduralMemory
from .semantic_memory import SemanticMemory
from .working_memory import WorkingMemory

__all__ = [
    "MemoryManager",
    "WorkingMemory",
    "EpisodicMemory",
    "SemanticMemory",
    "ProceduralMemory",
    "MemoryConsolidator"
]

__version__ = "2.0.0"