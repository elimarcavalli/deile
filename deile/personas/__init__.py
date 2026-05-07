"""Sistema modular de personas para DEILE 2.0 ULTRA

Este módulo implementa um sistema avançado de personas baseado nas melhores práticas
de agentes AI enterprise-grade de 2025, incluindo:

- Persona base abstrata para extensibilidade
- Manager de ciclo de vida com hot-reload
- Builder pattern para composição de personas
- Validação com Pydantic schemas
- Suporte a configuração YAML
"""

# Set alias for backward compatibility
from . import config
from .base import (AgentCapability, AgentContext, BaseAutonomousPersona,
                   BasePersona, PersonaConfig)
from .builder import PersonaBuilder
from .context import PersonaContext
from .loader import PersonaLoader
from .manager import PersonaManager
from .memory.integration import PersonaMemoryLayer

config.PersonaCapability = AgentCapability

__all__ = [
    "BasePersona",
    "BaseAutonomousPersona",
    "PersonaConfig",
    "AgentCapability",
    "AgentContext",
    "PersonaManager",
    "PersonaBuilder",
    "PersonaLoader",
    "PersonaContext",
    "PersonaMemoryLayer"
]

__version__ = "2.0.0"