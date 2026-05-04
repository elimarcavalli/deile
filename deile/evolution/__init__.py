"""Self-Improvement Engine para DEILE 2.0 ULTRA

Sistema de auto-melhoria baseado nas práticas mais avançadas de 2025:
- Self-analysis contínuo de performance
- Autonomous code modification
- Improvement loop com rollback pós-aplicação (sem validação em sandbox; ver issue #56)
- Benchmarking e métricas de progresso
- Rollback automático de melhorias falhadas

> Não há sandbox de validação para modificações geradas. O fluxo só
> deve ser ativado em ambientes experimentais (`ImprovementLoop.start(experimental=True)`).
> Ver issue #56.
"""

from .self_analyzer import SelfAnalyzer
from .code_modifier import CodeModifier
from .improvement_loop import ImprovementLoop
from .benchmarker import Benchmarker
from .rollback_manager import RollbackManager

__all__ = [
    "SelfAnalyzer",
    "CodeModifier",
    "ImprovementLoop",
    "Benchmarker",
    "RollbackManager",
]

__version__ = "2.0.0"
