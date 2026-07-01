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


