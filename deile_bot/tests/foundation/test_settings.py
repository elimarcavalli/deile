"""Tests for BotSettings."""

from __future__ import annotations

import pytest

from deile_bot.foundation.settings import (FoundationSettings,
                                           get_bot_settings,
                                           reset_bot_settings_cache)


@pytest.fixture(autouse=True)
def _reset_cache_and_env(monkeypatch):
    reset_bot_settings_cache()
    keys_to_clear = [
        "DEILE_BOT_INTENT_CLASSIFIER",
        "DEILE_BOT_DATA_RETENTION_DAYS",
        "DEILE_BOT_AGENT_BRIDGE_MODE",
    ]
    for k in keys_to_clear:
        monkeypatch.delenv(k, raising=False)
    yield
    reset_bot_settings_cache()


class TestFoundationSettings:
    def test_defaults(self):
        s = FoundationSettings()
        assert s.intent_classifier == "heuristic"
        assert s.rate_limit_user_burst == 5
        assert s.agent_bridge_mode == "in_process"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_INTENT_CLASSIFIER", "llm")
        s = FoundationSettings()
        assert s.intent_classifier == "llm"

    def test_invalid_classifier_rejected(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_INTENT_CLASSIFIER", "magic")
        with pytest.raises(Exception):
            FoundationSettings()

    def test_int_coerced_from_env(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_DATA_RETENTION_DAYS", "30")
        s = FoundationSettings()
        assert s.data_retention_days == 30


class TestGetBotSettings:
    def test_singleton_behavior(self):
        a = get_bot_settings()
        b = get_bot_settings()
        assert a is b

    def test_reset_cache(self, monkeypatch):
        a = get_bot_settings()
        reset_bot_settings_cache()
        monkeypatch.setenv("DEILE_BOT_INTENT_CLASSIFIER", "always_respond")
        b = get_bot_settings()
        assert b.foundation.intent_classifier == "always_respond"
        assert a is not b


class TestNoProvidersImportInFoundation:
    """Sanity: foundation must not import deile_bot.providers (F6)."""

    def test_no_provider_imports(self):
        import pathlib

        foundation_dir = pathlib.Path(__file__).parent.parent.parent / "foundation"
        for py in foundation_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "from deile_bot.providers" not in text, f"{py} imports providers"
