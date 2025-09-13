"""Sistema modular de personas para DEILE 2.0 ULTRA

Este módulo implementa um sistema avançado de personas baseado nas melhores práticas
de agentes AI enterprise-grade de 2025, incluindo:

- Persona base abstrata para extensibilidade
- Manager de ciclo de vida com hot-reload
- Builder pattern para composição de personas
- Validação com Pydantic schemas
- Suporte a configuração YAML
"""

from .base import BasePersona, PersonaConfig, PersonaCapability
from .manager import PersonaManager
from .builder import PersonaBuilder
from .loader import PersonaLoader

__all__ = [
    "BasePersona",
    "PersonaConfig",
    "PersonaCapability",
    "PersonaManager",
    "PersonaBuilder",
    "PersonaLoader"
]

__version__ = "2.0.0"