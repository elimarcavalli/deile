"""Sistema modular de personas para DEILE 2.0 ULTRA

Este módulo implementa um sistema avançado de personas baseado nas melhores práticas
de agentes AI enterprise-grade de 2025, incluindo:

- Persona base abstrata para extensibilidade
- Manager de ciclo de vida com hot-reload
- Builder pattern para composição de personas
- Validação com Pydantic schemas
- Suporte a configuração YAML
"""

from .base import BasePersona, PersonaConfig, PersonaCapability, BaseAutonomousPersona, AgentContext
from .manager import PersonaManager
from .builder import PersonaBuilder
from .loader import PersonaLoader
from .context import PersonaContext
from .memory.integration import PersonaMemoryLayer

__all__ = [
    "BasePersona",
    "BaseAutonomousPersona",
    "PersonaConfig",
    "PersonaCapability",
    "AgentContext",
    "PersonaManager",
    "PersonaBuilder",
    "PersonaLoader",
    "PersonaContext",
    "PersonaMemoryLayer"
]

__version__ = "2.0.0"