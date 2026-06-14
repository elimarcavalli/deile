"""Dependency Resolver - Resolução automática de dependências entre plugins"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Set

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
        """Detecta dependências circulares via DFS com marcação branco/cinza/preto."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {node: WHITE for node in self.dependency_graph}
        cycles: List[List[str]] = []

        def dfs(node: str, stack: List[str]) -> None:
            color[node] = GRAY
            stack.append(node)
            for dep in self.dependency_graph[node].dependencies:
                if dep not in self.dependency_graph:
                    continue
                if color[dep] == GRAY:
                    cycle_start = stack.index(dep)
                    cycles.append(stack[cycle_start:] + [dep])
                elif color[dep] == WHITE:
                    dfs(dep, stack)
            stack.pop()
            color[node] = BLACK

        for node in list(self.dependency_graph):
            if color[node] == WHITE:
                dfs(node, [])

        return cycles