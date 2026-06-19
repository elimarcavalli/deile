"""Unit tests for parse_decompose_result — issue #770.

Covers:
- AC1: sem keyword DECOMPOSTO:, auto-referência retorna []
- AC3: caminho estrito (DECOMPOSTO:) exclui auto-ref exata via parent_number
- AC4: retrocompatibilidade — sem parent_number, comportamento original
"""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.implementer import parse_decompose_result


class TestNoKeywordReturnsEmpty:
    """AC1: sem DECOMPOSTO:, qualquer #N (incluindo auto-ref) retorna [].

    O fallback foi removido (bug 2b639f8f / issue #770). Sem a keyword
    explícita no output do worker, não houve decomposição.
    """

    def test_ac1_no_keyword_self_reference_returns_empty(self):
        """Output menciona apenas a própria issue sem DECOMPOSTO: → []."""
        result = parse_decompose_result(
            "Originada de #768\nVer #768 para detalhes",
            parent_number=768,
        )
        assert result == []

    def test_no_keyword_mentions_higher_numbers_returns_empty(self):
        """Menciona issues com número > pai mas sem DECOMPOSTO: → []."""
        result = parse_decompose_result(
            "Mencionei contexto #500\nCriei #769 e #770",
            parent_number=768,
        )
        assert result == []

    def test_no_keyword_no_parent_number_returns_empty(self):
        """Sem parent_number e sem DECOMPOSTO: → []."""
        result = parse_decompose_result("#100 #200 #300")
        assert result == []


class TestStrictPathAutoReferenceFilter:
    """AC3: caminho estrito (DECOMPOSTO:) exclui auto-ref exata."""

    def test_ac3_strict_excludes_self_reference(self):
        """AC3: DECOMPOSTO: #768 #769 com parent_number=768 → [769]."""
        result = parse_decompose_result("DECOMPOSTO: #768 #769", parent_number=768)
        assert result == [769]

    def test_strict_all_others_kept(self):
        """parent_number não está na lista → retorna todos."""
        result = parse_decompose_result("DECOMPOSTO: #769 #770", parent_number=768)
        assert result == [769, 770]

    def test_strict_only_self_reference(self):
        """DECOMPOSTO: apenas com a própria issue → []."""
        result = parse_decompose_result("DECOMPOSTO: #42", parent_number=42)
        assert result == []


class TestBackwardCompatibility:
    """AC4: sem parent_number, comportamento original preservado."""

    def test_ac4_strict_no_parent_number(self):
        """AC4: DECOMPOSTO: #11 #12 sem parent_number → [11, 12]."""
        assert parse_decompose_result("DECOMPOSTO: #11 #12") == [11, 12]

    def test_ac4_no_keyword_no_parent_number_returns_empty(self):
        """AC4 (sem fallback): sem DECOMPOSTO:, retorna [] independente do conteúdo."""
        text = (
            "Criei as derivadas:\n"
            "- #401 — split A\n"
            "- #402 — split B\n"
        )
        assert parse_decompose_result(text) == []

    def test_ac4_decorated_decomposto_no_parent_number(self):
        """AC4: formatos decorados sem parent_number ainda funcionam."""
        assert parse_decompose_result("**DECOMPOSTO:** #11 #12") == [11, 12]
        assert parse_decompose_result("### DECOMPOSTO: #99") == [99]
        assert parse_decompose_result("> DECOMPOSTO: #5 e #6 (independentes)") == [5, 6]


