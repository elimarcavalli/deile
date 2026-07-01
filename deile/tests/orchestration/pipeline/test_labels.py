"""Unit tests for label name constants and helpers."""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.labels import (
    LABEL_COLORS, LABEL_DESCRIPTIONS, MENTION_DONE,
    MENTION_LABELS, REVIEW_LABELS, REVIEW_PENDING, WORKFLOW_BLOCKED,
    WORKFLOW_LABELS, WORKFLOW_NEW, batch_id_from_label,
    current_refine_attempt_from_labels, is_batch_label,
    is_refine_attempt_label, make_batch_label, make_refine_attempt_label,
    parse_refine_attempt_label)


class TestLabelConstants:
    @pytest.mark.parametrize(
        "labels, prefix",
        [
            (WORKFLOW_LABELS, "~workflow:"),
            (REVIEW_LABELS, "~review:"),
        ],
    )
    def test_label_sets_use_tilde_prefix(self, labels, prefix):
        for label in labels:
            assert label.startswith(prefix)

    def test_every_label_has_color_and_description(self):
        for label in (*WORKFLOW_LABELS, *REVIEW_LABELS, *MENTION_LABELS):
            assert label in LABEL_COLORS
            assert label in LABEL_DESCRIPTIONS
            # Colors are 6-digit hex (no #).
            assert len(LABEL_COLORS[label]) == 6
            int(LABEL_COLORS[label], 16)

    def test_mention_done_label_present(self):
        # Cross-tick dedup of sticky mention triggers (issue #253 storm fix):
        # the marker must be a mention label so ensure_pipeline_labels creates
        # it, with color + description.
        assert MENTION_DONE == "~mention:processado"
        assert MENTION_DONE in MENTION_LABELS
        assert MENTION_DONE in LABEL_COLORS
        assert MENTION_DONE in LABEL_DESCRIPTIONS

    def test_blocked_label_present(self):
        # Resume feature (issue #254): the block label must be a workflow label
        # so ensure_pipeline_labels creates it, with color + description.
        assert WORKFLOW_BLOCKED == "~workflow:bloqueada"
        assert WORKFLOW_BLOCKED in WORKFLOW_LABELS
        assert WORKFLOW_BLOCKED in LABEL_COLORS
        assert WORKFLOW_BLOCKED in LABEL_DESCRIPTIONS


class TestBatchHelpers:
    @pytest.mark.parametrize("batch_id", ["abc12345", "f00dcafe"])
    def test_batch_label_make_is_parse_roundtrip(self, batch_id):
        label = make_batch_label(batch_id)
        assert label == f"~batch:{batch_id}"
        assert is_batch_label(label)
        assert batch_id_from_label(label) == batch_id

    def test_is_batch_label_false_for_workflow(self):
        assert not is_batch_label(WORKFLOW_NEW)
        assert not is_batch_label(REVIEW_PENDING)

    def test_batch_id_from_label_rejects_non_batch(self):
        with pytest.raises(ValueError):
            batch_id_from_label(WORKFLOW_NEW)


class TestRefineAttemptHelpers:
    """Testa os helpers de label ~refine:N (issue R1 — contador durável de
    passes de refino)."""

    @pytest.mark.parametrize("n", [0, 5, 99])
    def test_make_refine_label_e_is_refine_attempt(self, n):
        label = make_refine_attempt_label(n)
        assert label == f"~refine:{n}"
        assert is_refine_attempt_label(label)

    def test_is_refine_attempt_label_falso_para_outros(self):
        assert not is_refine_attempt_label(WORKFLOW_NEW)
        assert not is_refine_attempt_label("~attempt:1")
        assert not is_refine_attempt_label("refine:1")        # sem ~
        assert not is_refine_attempt_label("~refine:")        # sem número
        assert not is_refine_attempt_label("~refinar:1")      # confusão com REFINAR
        assert not is_refine_attempt_label("~refine:1a")      # sufixo não numérico

    def test_parse_refine_attempt_label_extrai_n(self):
        assert parse_refine_attempt_label("~refine:0") == 0
        assert parse_refine_attempt_label("~refine:3") == 3
        assert parse_refine_attempt_label("~refine:42") == 42

    def test_parse_refine_attempt_label_levanta_para_invalido(self):
        with pytest.raises(ValueError):
            parse_refine_attempt_label("~attempt:1")
        with pytest.raises(ValueError):
            parse_refine_attempt_label(WORKFLOW_NEW)
        with pytest.raises(ValueError):
            parse_refine_attempt_label("~refine:")

    def test_current_refine_zero_sem_label(self):
        """Sem nenhuma label ~refine:N, retorna 0."""
        assert current_refine_attempt_from_labels([]) == 0
        assert current_refine_attempt_from_labels(None) == 0
        assert current_refine_attempt_from_labels(["~workflow:nova", "refinar"]) == 0

    def test_current_refine_maior_com_multiplas(self):
        """Quando há múltiplas labels (edge case pós-race), retorna o maior N."""
        labels = ["~refine:2", "~refine:4", "~refine:1"]
        assert current_refine_attempt_from_labels(labels) == 4

    def test_current_refine_ignora_labels_irrelevantes(self):
        """Labels não-refine não interferem no resultado."""
        labels = ["~workflow:em_arquitetura", "refinar", "~refine:3", "~attempt:2"]
        assert current_refine_attempt_from_labels(labels) == 3

    def test_current_refine_valor_unico(self):
        """Caso comum: exatamente uma label ~refine:N."""
        assert current_refine_attempt_from_labels(["~refine:5"]) == 5
