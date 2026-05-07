"""Root conftest for the deile test suite.

Resets the Settings singleton before and after each test so that
monkeypatch.setenv / monkeypatch.delenv changes are always picked up
by modules that call get_settings().
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    from deile.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()
