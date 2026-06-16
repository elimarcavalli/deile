"""DEILE AI AGENT"""

# A versão é fonte única em ``deile/__version__.py`` (submódulo homônimo).
# NÃO rebindar ``__version__`` aqui como string: isso sobrescreveria o atributo
# ``deile.__version__`` (que aponta para o submódulo) e quebraria todo consumidor
# que faz ``import deile.__version__`` / ``from deile.__version__ import ...``.
# Consumidores leem a versão de ``deile.__version__.__version__``.

__author__ = "@elimarcavalli"
__description__ = "Agente de IA CLI para desenvolvimento autônomo de software."

from .core.agent import DeileAgent
from .core.exceptions import DEILEError

__all__ = ["DeileAgent", "DEILEError"]
