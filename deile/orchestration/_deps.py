"""Internal helper for orchestration dependency-completion checks.

Centraliza a heurística (antes duplicada em ``plan_manager`` e
``sqlite_task_manager``) de verificar se *todas* as dependências de um
item — step de plano ou task — já foram concluídas. Helper interno e
genérico do subpacote ``orchestration`` — não exposto por nenhum
registry.
"""

from typing import Callable, Iterable, Optional, TypeVar

__all__ = ["all_dependencies_met"]

_Id = TypeVar("_Id")
_Item = TypeVar("_Item")


def all_dependencies_met(
    depends_on: Iterable[_Id],
    lookup: Callable[[_Id], Optional[_Item]],
    is_completed: Callable[[_Item], bool],
) -> bool:
    """Diz se todas as dependências de um item estão concluídas.

    Itera sobre os ids em ``depends_on``, resolve cada um para o item
    dependente via ``lookup`` e verifica sua conclusão via
    ``is_completed``. Uma dependência cujo id não resolve (``lookup``
    devolve ``None``) é tratada como **não cumprida** — espelhando o
    comportamento original de ambos os call-sites.

    Args:
        depends_on: Iterável de ids das dependências.
        lookup: Resolve um id para o item dependente, ou ``None`` se o
            item não for encontrado.
        is_completed: Predicado que diz se um item dependente está
            concluído.

    Returns:
        ``True`` se *todas* as dependências resolvem para itens
        concluídos; ``False`` se alguma não resolve ou não está
        concluída.
    """
    for dep_id in depends_on:
        dep_item = lookup(dep_id)
        if dep_item is None or not is_completed(dep_item):
            return False
    return True
