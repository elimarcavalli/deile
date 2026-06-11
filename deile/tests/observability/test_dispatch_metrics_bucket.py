"""AC4 — boundary values do ``_tool_burst_bucket`` — issue #455."""

from __future__ import annotations

import pytest

from deile.observability import dispatch_metrics as dm

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "count,expected",
    [
        (10, "50-"),
        (49, "50-"),
        (50, "100-"),
        (99, "100-"),
        (100, "500+"),
        (1000, "500+"),
    ],
)
def test_bucket_boundaries(count, expected):
    assert dm._tool_burst_bucket(count) == expected


def test_bucket_note_documents_500_offset():
    """_BUCKET_NOTE documenta que '500+' inicia em 100, não 500."""
    assert "500+" in dm._BUCKET_NOTE
    assert "100" in dm._BUCKET_NOTE
