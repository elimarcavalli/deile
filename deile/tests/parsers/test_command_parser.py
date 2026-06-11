"""Regressão de contrato para o CommandParser.

Trava o comportamento observado hoje (sem `config_manager`, isto é, sem
acesso ao CommandRegistry): qualquer `/<token>` casa, `parse()` devolve um
único `ParsedCommand` com `action="slash_<nome>"` e nunca deixa exceção
escapar.
"""

import pytest

from deile.parsers.base import ParsedCommand, ParseResult, ParseStatus
from deile.parsers.command_parser import CommandParser


@pytest.fixture
def parser() -> CommandParser:
    return CommandParser()


@pytest.mark.unit
def test_metadata_contract(parser: CommandParser):
    assert parser.name == "command_parser"
    assert parser.priority == 90
    assert parser.patterns  # padrões declarados não podem ficar vazios


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/help", True),
        ("   /status", True),  # whitespace à esquerda é tolerado
        ("/DOC-HYGIENE arg", True),  # hífen é parte do nome
        ("hello there", False),
        ("not /a/command", False),  # só conta quando começa com '/'
    ],
)
def test_can_parse(parser: CommandParser, text: str, expected: bool):
    assert parser.can_parse(text) is expected


@pytest.mark.unit
def test_parse_command_without_args(parser: CommandParser):
    result = parser.parse("/help")
    assert result.status is ParseStatus.SUCCESS
    assert len(result.commands) == 1
    cmd = result.commands[0]
    assert isinstance(cmd, ParsedCommand)
    assert cmd.action == "slash_help"
    assert cmd.target is None
    assert cmd.arguments == {"command_name": "help", "use_command_system": True}


@pytest.mark.unit
def test_parse_command_with_args(parser: CommandParser):
    result = parser.parse("/DOC-HYGIENE arg1 arg2")
    cmd = result.commands[0]
    assert result.status is ParseStatus.SUCCESS
    assert cmd.action == "slash_doc-hygiene"  # nome normalizado para minúsculo
    assert cmd.target == "arg1 arg2"
    assert cmd.arguments["raw_args"] == "arg1 arg2"
    assert cmd.arguments["use_command_system"] is True


@pytest.mark.unit
def test_parse_non_command_returns_failed(parser: CommandParser):
    result = parser.parse("just plain text")
    assert result.status is ParseStatus.FAILED
    assert result.error_message
    assert not result.commands


@pytest.mark.unit
def test_get_confidence(parser: CommandParser):
    assert parser.get_confidence("/help") == pytest.approx(0.95)
    assert parser.get_confidence("not a command") == 0.0


@pytest.mark.unit
def test_parse_never_raises(parser: CommandParser):
    """parse() captura qualquer falha e devolve ParseResult, nunca lança."""
    for weird in ["/", "/\\", "/" + "x" * 5000, ""]:
        result = parser.parse(weird)
        assert isinstance(result, ParseResult)
        assert result.status in (ParseStatus.SUCCESS, ParseStatus.FAILED)
