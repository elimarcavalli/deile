"""DEILE AI AGENT"""

__version__ = "5.1.0"
__author__ = "@elimarcavalli"
__description__ = "Agente de IA CLI para desenvolvimento autônomo de software."

from .core.agent import DeileAgent
from .core.exceptions import DEILEError

__all__ = ["DeileAgent", "DEILEError"]