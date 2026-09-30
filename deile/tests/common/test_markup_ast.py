"""Contrato de ``MarkupAST.from_plain`` — o deilebot monta com ele o AST de toda mensagem do Discord."""

from __future__ import annotations

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind


def test_from_plain_wraps_text_in_one_plain_span_without_parsing_markdown():
    text = "olá *não é negrito* `nem código`"

    ast = MarkupAST.from_plain(text)

    assert list(ast) == [MarkupSpan(kind=SpanKind.PLAIN, text=text)]
    assert ast.to_plain() == text


def test_from_plain_empty_text_yields_empty_ast():
    assert MarkupAST.from_plain("") == MarkupAST()
