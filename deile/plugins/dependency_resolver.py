"""Dependency Resolver - Resolução automática de dependências entre plugins"""

import logging
from typing import Dict, List, Set, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DependencyNode:
    """Nó no grafo de dependências"""
    plugin_id: str
    dependencies: Set[str]
    dependents: Set[str]
    resolved: bool = False


class DependencyResolver:
    """Resolve dependências entre plugins usando topological sort"""

    def __init__(self):
        self.dependency_graph: Dict[str, DependencyNode] = {}

    def add_plugin(self, plugin_id: str, dependencies: List[str]) -> None:
        """Adiciona plugin ao grafo de dependências"""
        if plugin_id not in self.dependency_graph:
            self.dependency_graph[plugin_id] = DependencyNode(
                plugin_id=plugin_id,
                dependencies=set(dependencies),
                dependents=set()
            )

            # Atualiza dependents nos plugins dependidos
            for dep_id in dependencies:
                if dep_id not in self.dependency_graph:
                    self.dependency_graph[dep_id] = DependencyNode(
                        plugin_id=dep_id,
                        dependencies=set(),
                        dependents=set()
                    )
                self.dependency_graph[dep_id].dependents.add(plugin_id)

    def resolve_load_order(self, plugins: List[str]) -> List[str]:
        """Resolve ordem de carregamento usando topological sort"""
        # Cria cópia do grafo para não modificar original
        graph = {k: DependencyNode(
            plugin_id=v.plugin_id,
            dependencies=v.dependencies.copy(),
            dependents=v.dependents.copy()
        ) for k, v in self.dependency_graph.items()}

        load_order = []
        queue = []

        # Adiciona plugins sem dependências à queue inicial
        for plugin_id in plugins:
            if plugin_id in graph and not graph[plugin_id].dependencies:
                queue.append(plugin_id)

        while queue:
            current = queue.pop(0)
            load_order.append(current)

            # Remove current das dependências dos seus dependents
            if current in graph:
                for dependent in graph[current].dependents.copy():
                    if dependent in graph:
                        graph[dependent].dependencies.discard(current)

                        # Se dependent não tem mais dependências, adiciona à queue
                        if not graph[dependent].dependencies and dependent in plugins:
                            queue.append(dependent)

        return load_order

    def check_circular_dependencies(self) -> List[List[str]]:
        """Detecta dependências circulares"""
        # Implementação básica - pode ser expandida
        circular_deps = []
        # TODO: Implementar detecção de ciclos no grafo
        return circular_deps