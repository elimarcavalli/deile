"""Unit tests for parse_decompose_result — issue #770.

Covers:
- AC1: fallback descarta auto-referência (n == parent_number)
- AC2: fallback descarta n <= parent_number, mantém n > parent_number
- AC3: caminho estrito (DECOMPOSTO:) exclui auto-ref exata
- AC4: retrocompatibilidade — sem parent_number, comportamento original
- AC7: warning log quando fallback descarta n <= parent_number
"""

from __future__ import annotations

import logging

import pytest

from deile.orchestration.pipeline.implementer import parse_decompose_result


class TestFallbackAutoReferenceFilter:
    """AC1 e AC2: fallback descarta n <= parent_number."""

    def test_ac1_fallback_discards_self_reference(self):
        """AC1: output menciona apenas a própria issue — fallback retorna []."""
        result = parse_decompose_result(
            "Originada de #768\nVer #768 para detalhes",
            parent_number=768,
        )
        assert result == []

    def test_ac2_fallback_filters_leq_parent_keeps_gt(self):
        """AC2: #500 (<=768) descartado; #769 e #770 (>768) mantidos."""
        result = parse_decompose_result(
            "Mencionei contexto #500\nCriei #769 e #770",
            parent_number=768,
        )
        assert result == [769, 770]

    def test_fallback_all_below_parent_returns_empty(self):
        """Todos os números são <= pai — retorna []."""
        result = parse_decompose_result(
            "#100 #200 #300",
            parent_number=500,
        )
        assert result == []

    def test_fallback_mix_filters_correctly(self):
        """Mix de n<=pai e n>pai: só n>pai sobrevive."""
        result = parse_decompose_result(
            "#100 #500 #501 #600",
            parent_number=500,
        )
        assert result == [501, 600]


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

    def test_ac4_fallback_no_parent_number_collects_all(self):
        """AC4: fallback sem parent_number coleta todos os #N."""
        text = (
            "Criei as derivadas:\n"
            "- #401 — split A\n"
            "- #402 — split B\n"
        )
        assert parse_decompose_result(text) == [401, 402]

    def test_ac4_decorated_decomposto_no_parent_number(self):
        """AC4: formatos decorados sem parent_number ainda funcionam."""
        assert parse_decompose_result("**DECOMPOSTO:** #11 #12") == [11, 12]
        assert parse_decompose_result("### DECOMPOSTO: #99") == [99]
        assert parse_decompose_result("> DECOMPOSTO: #5 e #6 (independentes)") == [5, 6]


class TestWarningLog:
    """AC7: warning emitido quando fallback descarta n <= parent_number."""

    def test_ac7_warning_emitted_on_discard(self, caplog):
        """AC7: fallback descarta [768] — warning com pai e descartados."""
        with caplog.at_level(logging.WARNING, logger="deile.orchestration.pipeline.implementer"):
            parse_decompose_result(
                "Originada de #768\nVer #768 para detalhes",
                parent_number=768,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"Esperado 1 warning, obteve {len(warnings)}: {[r.message for r in warnings]}"
        msg = warnings[0].getMessage()
        assert "768" in msg, f"Warning deve mencionar pai #768; obteve: {msg}"
        assert "descartou" in msg.lower() or "descart" in msg.lower() or "768" in msg

    def test_ac7_no_warning_when_no_discard(self, caplog):
        """Sem descarte, nenhum warning emitido."""
        with caplog.at_level(logging.WARNING, logger="deile.orchestration.pipeline.implementer"):
            parse_decompose_result(
                "Criei #769 e #770",
                parent_number=768,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "descartou" in r.getMessage()]
        assert warnings == []

    def test_ac7_no_warning_without_parent_number(self, caplog):
        """Sem parent_number, fallback não emite warning (comportamento original)."""
        with caplog.at_level(logging.WARNING, logger="deile.orchestration.pipeline.implementer"):
            parse_decompose_result("Ver #100 #200 para contexto")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "descartou" in r.getMessage()]
        assert warnings == []
