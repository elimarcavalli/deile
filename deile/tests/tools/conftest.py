"""Shared fixtures for tools tests.

Resets the Settings singleton around tests that manipulate DEILE_* env vars
so that monkeypatch.setenv changes are actually picked up.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    from deile.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()
