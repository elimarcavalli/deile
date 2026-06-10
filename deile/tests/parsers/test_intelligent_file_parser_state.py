"""Documenta o estado atual do IntelligentFileParser.

`IntelligentFileParser` herda de `Parser` mas NÃO implementa os membros
abstratos `parse` e `patterns` — portanto é abstrato e nunca pode ser
instanciado nem registrado pela auto-descoberta (o filtro
`not inspect.isabstract(obj)` em `ParserRegistry._discover_in_package` o
ignora silenciosamente).

Este teste trava esse fato. Se um follow-up tornar a classe concreta
(implementando os abstratos), este teste falha de propósito, forçando uma
decisão consciente sobre ativá-lo no registry (o que MUDA o conjunto de
parsers ativos e o comportamento de parsing). Ver FU registrada na issue
de rastreamento.
"""

import inspect

import pytest

from deile.parsers.base import Parser
from deile.parsers.intelligent_file_parser import IntelligentFileParser


@pytest.mark.unit
def test_is_a_parser_subclass():
    assert issubclass(IntelligentFileParser, Parser)


@pytest.mark.unit
def test_is_currently_abstract_and_inert():
    assert inspect.isabstract(IntelligentFileParser)
    assert IntelligentFileParser.__abstractmethods__ == frozenset(
        {"parse", "patterns"}
    )
    with pytest.raises(TypeError):
        IntelligentFileParser()  # type: ignore[abstract]


@pytest.mark.unit
def test_auto_discovery_skips_abstract_parser_without_crashing():
    from deile.parsers.registry import ParserRegistry

    registry = ParserRegistry()
    discovered = registry._discover_in_package(
        "deile.parsers.intelligent_file_parser"
    )
    assert discovered == 0
    assert "intelligent_file_parser" not in registry
