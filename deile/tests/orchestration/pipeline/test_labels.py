"""Unit tests for label name constants and helpers."""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.labels import (BATCH_LABEL_PREFIX,
                                                 LABEL_COLORS,
                                                 LABEL_DESCRIPTIONS,
                                                 REVIEW_CONCLUDED,
                                                 REVIEW_LABELS,
                                                 REVIEW_PENDING,
                                                 WORKFLOW_LABELS, WORKFLOW_NEW,
                                                 batch_id_from_label,
                                                 is_batch_label,
                                                 make_batch_label)


class TestLabelConstants:
    def test_workflow_labels_use_tilde_prefix(self):
        for label in WORKFLOW_LABELS:
            assert label.startswith("~workflow:")

    def test_review_labels_use_tilde_prefix(self):
        for label in REVIEW_LABELS:
            assert label.startswith("~review:")

    def test_batch_prefix(self):
        assert BATCH_LABEL_PREFIX == "~batch:"

    def test_every_label_has_color_and_description(self):
        for label in (*WORKFLOW_LABELS, *REVIEW_LABELS):
            assert label in LABEL_COLORS
            assert label in LABEL_DESCRIPTIONS
            # Colors are 6-digit hex (no #).
            assert len(LABEL_COLORS[label]) == 6
            int(LABEL_COLORS[label], 16)


class TestBatchHelpers:
    def test_make_batch_label_uses_prefix(self):
        assert make_batch_label("abc12345") == "~batch:abc12345"

    def test_is_batch_label_true_for_prefixed(self):
        assert is_batch_label("~batch:abc12345")

    def test_is_batch_label_false_for_workflow(self):
        assert not is_batch_label(WORKFLOW_NEW)
        assert not is_batch_label(REVIEW_PENDING)

    def test_batch_id_from_label_extracts_id(self):
        assert batch_id_from_label("~batch:f00dcafe") == "f00dcafe"

    def test_batch_id_from_label_rejects_non_batch(self):
        with pytest.raises(ValueError):
            batch_id_from_label(WORKFLOW_NEW)
