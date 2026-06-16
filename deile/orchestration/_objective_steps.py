"""Internal helper for objective-to-steps heuristic derivation.

Centraliza a heurística mockup (antes duplicada em
``PlanManager._generate_steps_from_objective`` e
``WorkflowExecutor._analyze_objective_to_steps``) que mapeia palavras-chave
do objetivo textual para invocações de tools. Helper interno do subpacote
``orchestration`` — não exposto por nenhum registry nem importado por SDK
externo.

A função :func:`derive_step_specs` devolve uma representação **neutra**
(lista de :class:`StepSpec`) que cada gerador adapta ao seu dataclass
concreto (``PlanStep`` ou ``WorkflowStep``). A heurística keyword->tool
vive apenas aqui — os dois geradores não podem mais duplicá-la.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["StepSpec", "derive_step_specs"]


@dataclass
class StepSpec:
    """Spec neutra de um step derivado de um objetivo.

    Não acoplada a ``PlanStep`` nem a ``WorkflowStep``: carrega apenas os
    dados comuns a ambos os geradores. ``risk_level``, ``requires_approval``
    e ``security_level`` são valores neutros consumidos só pelo
    ``PlanManager``; o ``WorkflowExecutor`` simplesmente os ignora.
    """

    tool_name: str
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    timeout: int = 300
    # Dados extras consumidos somente pelo PlanManager (neutros aqui).
    risk_level: str = "low"
    requires_approval: bool = False
    # security_level só é propagado para os params da tool pela adaptação
    # do PlanManager; o WorkflowExecutor o ignora, preservando o default
    # "moderate" de bash_tool no caminho do WorkflowExecutor.
    security_level: Optional[str] = None


# Tabela única keyword->tool. Cada entrada usa o superset coerente das duas
# implementações originais (PlanManager testava menos keywords; o superset
# não quebra testes existentes e elimina a divergência silenciosa).
def derive_step_specs(objective: str) -> List[StepSpec]:
    """Deriva specs neutras de steps a partir de um objetivo textual.

    Função pura e síncrona. Replica fielmente a heurística mockup das duas
    implementações originais, unindo as tabelas de keywords e o superset
    dos params. A ordem dos steps é a mesma das implementações originais:
    read_file, list_files, find_in_files, bash_execute, validation. Se
    nenhuma keyword casar, devolve um único step genérico ``list_files``.

    Args:
        objective: Objetivo textual descrito pelo usuário.

    Returns:
        Lista de :class:`StepSpec` na ordem de derivação.
    """
    specs: List[StepSpec] = []
    objective_lower = objective.lower()

    if any(w in objective_lower for w in ("file", "read", "analyze", "check")):
        specs.append(
            StepSpec(
                tool_name="read_file",
                params={"path": "README.md"},
                description="Read target file",
                timeout=30,
                risk_level="low",
            )
        )

    if any(w in objective_lower for w in ("list", "files", "directory", "explore")):
        specs.append(
            StepSpec(
                tool_name="list_files",
                params={"path": ".", "recursive": True},
                description="List files in directory",
                timeout=60,
                risk_level="low",
            )
        )

    if any(w in objective_lower for w in ("search", "find", "grep", "pattern")):
        specs.append(
            StepSpec(
                tool_name="find_in_files",
                params={
                    "pattern": "TODO",
                    "path": ".",
                    "max_context_lines": 5,
                    "max_results": 50,
                },
                description="Search for pattern in files",
                timeout=120,
                risk_level="low",
            )
        )

    if any(w in objective_lower for w in ("run", "execute", "command", "script")):
        specs.append(
            StepSpec(
                tool_name="bash_execute",
                params={
                    "command": "echo 'Hello World'",
                    "show_cli": True,
                },
                description="Execute command",
                timeout=300,
                risk_level="medium",
                requires_approval=True,
                security_level="safe",
            )
        )

    if any(w in objective_lower for w in ("validate", "verify", "check", "test")):
        specs.append(
            StepSpec(
                tool_name="validation",
                params={"validation_type": "general"},
                description="Validate workflow results",
                timeout=60,
                risk_level="low",
            )
        )

    if not specs:
        specs.append(
            StepSpec(
                tool_name="list_files",
                params={"path": ".", "recursive": False},
                description=f"General analysis for: {objective}",
                timeout=60,
                risk_level="low",
            )
        )

    return specs
