"""DEILE - Sistema de Agente de IA Modular e Extensível"""

__version__ = "4.0.0"
__author__ = "DEILE Team"
__description__ = "Agente de IA CLI para desenvolvimento de software"

from .core.agent import DeileAgent
from .core.exceptions import DEILEError

__all__ = ["DeileAgent", "DEILEError"]