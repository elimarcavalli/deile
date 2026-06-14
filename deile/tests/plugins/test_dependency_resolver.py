"""Tests for DependencyResolver.check_circular_dependencies (issue #684)."""

import pytest

from deile.plugins.dependency_resolver import DependencyNode, DependencyResolver


def _build(edges: list[tuple[str, list[str]]]) -> DependencyResolver:
    """Build a DependencyResolver with an explicit edge list, bypassing the
    add_plugin guard that prevents updating already-created nodes.
    This lets us construct cycles directly, which is what check_circular_dependencies must handle."""
    resolver = DependencyResolver()
    # First pass: ensure all nodes exist
    all_nodes = set()
    for plugin_id, deps in edges:
        all_nodes.add(plugin_id)
        all_nodes.update(deps)
    for node in all_nodes:
        resolver.dependency_graph[node] = DependencyNode(
            plugin_id=node, dependencies=set(), dependents=set()
        )
    # Second pass: wire dependencies and dependents
    for plugin_id, deps in edges:
        resolver.dependency_graph[plugin_id].dependencies = set(deps)
        for dep in deps:
            resolver.dependency_graph[dep].dependents.add(plugin_id)
    return resolver


@pytest.mark.unit
def test_simple_cycle_a_b_a():
    """Ciclo simples A→B→A é detectado."""
    resolver = _build([("A", ["B"]), ("B", ["A"])])
    cycles = resolver.check_circular_dependencies()
    assert len(cycles) == 1
    cycle = cycles[0]
    assert set(cycle) >= {"A", "B"}


@pytest.mark.unit
def test_chain_cycle_a_b_c_a():
    """Ciclo em cadeia A→B→C→A é detectado."""
    resolver = _build([("A", ["B"]), ("B", ["C"]), ("C", ["A"])])
    cycles = resolver.check_circular_dependencies()
    assert len(cycles) == 1
    cycle = cycles[0]
    assert set(cycle) >= {"A", "B", "C"}


@pytest.mark.unit
def test_acyclic_returns_empty():
    """Grafo acíclico A→B→C retorna []."""
    resolver = _build([("A", ["B"]), ("B", ["C"]), ("C", [])])
    assert resolver.check_circular_dependencies() == []


@pytest.mark.unit
def test_two_independent_cycles():
    """Dois ciclos independentes no mesmo grafo retornam ambos."""
    resolver = _build([
        ("A", ["B"]), ("B", ["A"]),
        ("X", ["Y"]), ("Y", ["X"]),
    ])
    cycles = resolver.check_circular_dependencies()
    assert len(cycles) == 2
    nodes_in_cycles = {node for cycle in cycles for node in cycle}
    assert {"A", "B"} <= nodes_in_cycles
    assert {"X", "Y"} <= nodes_in_cycles


@pytest.mark.unit
def test_missing_dependency_node_no_exception():
    """Aresta para nó ausente do grafo NÃO levanta exceção."""
    resolver = DependencyResolver()
    resolver.dependency_graph["A"] = DependencyNode(
        plugin_id="A", dependencies={"MISSING"}, dependents=set()
    )
    cycles = resolver.check_circular_dependencies()
    assert cycles == []


@pytest.mark.unit
def test_empty_graph_returns_empty():
    """Grafo vazio retorna []."""
    resolver = DependencyResolver()
    assert resolver.check_circular_dependencies() == []


@pytest.mark.unit
def test_self_loop():
    """Auto-dependência A→A é detectada como ciclo."""
    resolver = _build([("A", ["A"])])
    cycles = resolver.check_circular_dependencies()
    assert len(cycles) == 1
    assert "A" in cycles[0]
