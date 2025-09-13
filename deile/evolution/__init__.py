"""Self-Improvement Engine para DEILE 2.0 ULTRA

Sistema de auto-melhoria baseado nas práticas mais avançadas de 2025:
- Self-analysis contínuo de performance
- Autonomous code modification em sandbox
- Improvement loop com validação automática
- Benchmarking e métricas de progresso
- Safety sandbox para modificações seguras
- Rollback automático de melhorias falhadas
"""

from .self_analyzer import SelfAnalyzer
from .code_modifier import CodeModifier
from .improvement_loop import ImprovementLoop
from .benchmarker import Benchmarker
from .safety_sandbox import SafetySandbox
from .rollback_manager import RollbackManager

__all__ = [
    "SelfAnalyzer",
    "CodeModifier",
    "ImprovementLoop",
    "Benchmarker",
    "SafetySandbox",
    "RollbackManager"
]

__version__ = "2.0.0"