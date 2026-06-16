"""Regressão de contrato para o ParserRegistry.

Cobre o ponto de orquestração de parsing (resolução por prioridade, cache,
isolamento de exceções, enable/disable) usando parsers leves que não exigem
chaves de API nem I/O — sem gasto de token.
"""

from typing import List

import pytest

from deile.parsers.base import ParsedCommand, Parser, ParseResult, ParseStatus
from deile.parsers.command_parser import CommandParser
from deile.parsers.diff_parser import DiffParser
from deile.parsers.registry import ParserRegistry


class _BoomParser(Parser):
    """Parser que sempre lança — usado para verificar isolamento."""

    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "always raises"

    @property
    def patterns(self) -> List[str]:
        return [r"boom"]

    def can_parse(self, input_text: str) -> bool:
        return "boom" in input_text

    def parse(self, input_text: str) -> ParseResult:
        raise RuntimeError("kaboom")


class _LowPriorityParser(Parser):
    @property
    def name(self) -> str:
        return "low_prio"

    @property
    def description(self) -> str:
        return "matches anything, low priority"

    @property
    def patterns(self) -> List[str]:
        return [r".*"]

    @property
    def priority(self) -> int:
        return 1

    def can_parse(self, input_text: str) -> bool:
        return True

    def parse(self, input_text: str) -> ParseResult:
        return ParseResult(
            status=ParseStatus.SUCCESS,
            commands=[ParsedCommand(action="low", raw_text=input_text)],
            metadata={"parser": self.name},
        )


@pytest.fixture
def registry() -> ParserRegistry:
    reg = ParserRegistry()
    reg.disable_auto_discovery()
    return reg


@pytest.mark.unit
async def test_empty_input_fails_fast(registry: ParserRegistry):
    registry.register(CommandParser())
    result = await registry.parse("")
    assert result.status is ParseStatus.FAILED
    assert "Empty input" in result.error_message


@pytest.mark.unit
async def test_no_enabled_parsers_fails(registry: ParserRegistry):
    result = await registry.parse("anything")
    assert result.status is ParseStatus.FAILED
    assert "No enabled parsers" in result.error_message


@pytest.mark.unit
async def test_priority_order_in_list_all(registry: ParserRegistry):
    registry.register(DiffParser())  # priority 70
    registry.register(CommandParser())  # priority 90
    names = [p.name for p in registry.list_all()]
    assert names == ["command_parser", "diff_parser"]


@pytest.mark.unit
async def test_slash_command_routes_to_command_parser(registry: ParserRegistry):
    registry.register(CommandParser())
    registry.register(DiffParser())
    result = await registry.parse("/help")
    assert result.status is ParseStatus.SUCCESS
    assert result.metadata.get("parser") == "command_parser"


@pytest.mark.unit
async def test_parser_exception_is_isolated(registry: ParserRegistry):
    """Uma exceção de um parser não pode escapar do registry."""
    registry.register(_BoomParser())
    result = await registry.parse("boom now")
    assert result.status is ParseStatus.FAILED
    assert "boom" in result.error_message


@pytest.mark.unit
async def test_disabled_parser_does_not_participate(registry: ParserRegistry):
    registry.register(_LowPriorityParser())
    assert registry.disable_parser("low_prio") is True
    assert registry.list_enabled() == []
    result = await registry.parse("anything")
    assert result.status is ParseStatus.FAILED  # nenhum habilitado restou


@pytest.mark.unit
async def test_cache_returns_same_result(registry: ParserRegistry):
    registry.register(CommandParser())
    first = await registry.parse("/help")
    second = await registry.parse("/help")
    assert first is second  # segunda chamada vem do cache
    assert registry.get_stats()["cache_size"] >= 1


@pytest.mark.unit
async def test_disable_cache_clears_entries(registry: ParserRegistry):
    registry.register(CommandParser())
    await registry.parse("/help")
    registry.disable_cache()
    assert registry.get_stats()["cache_size"] == 0
    first = await registry.parse("/help")
    second = await registry.parse("/help")
    assert first is not second  # sem cache, instâncias distintas


@pytest.mark.unit
async def test_register_rejects_non_parser(registry: ParserRegistry):
    from deile.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        registry.register(object())  # type: ignore[arg-type]


@pytest.mark.unit
async def test_register_rejects_duplicate_name(registry: ParserRegistry):
    from deile.core.exceptions import ParserError

    registry.register(CommandParser())
    with pytest.raises(ParserError):
        registry.register(CommandParser())
