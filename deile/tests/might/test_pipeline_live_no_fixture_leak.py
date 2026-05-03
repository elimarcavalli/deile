"""Regression test for issue #45.

Ensures test_bot_pipeline_live.py contains no async def test_* functions that
pytest would collect and fail with 'fixture not found'.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.mark.unit
def test_bot_pipeline_live_has_no_pytest_collectible_async_test_funcs():
    src_path = Path(__file__).parent / "test_bot_pipeline_live.py"
    tree = ast.parse(src_path.read_text())

    leaked = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_")
    ]

    assert not leaked, (
        f"Found async def test_* functions in test_bot_pipeline_live.py that pytest "
        f"would collect and fail with 'fixture not found': {leaked}"
    )
