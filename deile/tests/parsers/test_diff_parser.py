"""Regressão de contrato para o DiffParser.

Documenta o comportamento atual da implementação básica: detecta marcadores
de diff, extrai os nomes de arquivo das linhas ``+++``/``---`` e devolve
SUCCESS (com comando ``apply_diff``) ou PARTIAL (apenas marcadores, sem
cabeçalho de arquivo). Nunca deixa exceção escapar.
"""

import pytest

from deile.parsers.base import ParseResult, ParseStatus
from deile.parsers.diff_parser import DiffParser

UNIFIED_DIFF = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
-old
+new
"""


@pytest.fixture
def parser() -> DiffParser:
    return DiffParser()


@pytest.mark.unit
def test_metadata_contract(parser: DiffParser):
    assert parser.name == "diff_parser"
    assert parser.priority == 70
    assert parser.patterns


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,expected",
    [
        (UNIFIED_DIFF, True),
        ("@@ -1 +1 @@", True),
        ("diff --git a/x b/x", True),
        ("plain prose with no markers", False),
        ("", False),
    ],
)
def test_can_parse(parser: DiffParser, text: str, expected: bool):
    assert parser.can_parse(text) is expected


@pytest.mark.unit
def test_parse_full_diff_is_success(parser: DiffParser):
    result = parser.parse(UNIFIED_DIFF)
    assert result.status is ParseStatus.SUCCESS
    assert result.commands
    cmd = result.commands[0]
    assert cmd.action == "apply_diff"
    assert cmd.arguments["diff_content"] == UNIFIED_DIFF
    assert result.file_references  # nomes extraídos das linhas +++/---
    assert result.tool_requests == ["diff_tool"]


@pytest.mark.unit
def test_parse_markers_only_is_partial(parser: DiffParser):
    """Marcador de hunk sem cabeçalho de arquivo => PARTIAL, sem comandos."""
    result = parser.parse("@@ -1,2 +1,2 @@")
    assert result.status is ParseStatus.PARTIAL
    assert not result.commands
    assert result.confidence == pytest.approx(0.3)


@pytest.mark.unit
def test_parse_non_diff_returns_failed(parser: DiffParser):
    result = parser.parse("this is not a diff at all")
    assert result.status is ParseStatus.FAILED
    assert result.error_message


@pytest.mark.unit
def test_get_confidence_scales_with_markers(parser: DiffParser):
    assert parser.get_confidence("not a diff") == 0.0
    full = parser.get_confidence(UNIFIED_DIFF)
    sparse = parser.get_confidence("@@ -1 +1 @@")
    assert full > sparse > 0.0


@pytest.mark.unit
def test_parse_never_raises(parser: DiffParser):
    for weird in ["", "+++", "@@" * 1000, UNIFIED_DIFF]:
        result = parser.parse(weird)
        assert isinstance(result, ParseResult)
        assert result.status in (
            ParseStatus.SUCCESS,
            ParseStatus.PARTIAL,
            ParseStatus.FAILED,
        )
