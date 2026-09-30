"""Integration: real Anthropic API call — skipped if ANTHROPIC_API_KEY absent."""

from __future__ import annotations

import os

import pytest

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not ANTHROPIC_API_KEY,
        reason="ANTHROPIC_API_KEY not set — skipping real API test",
    ),
]


